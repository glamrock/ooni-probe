import os

from ooni.managers import ReportEntryManager, MeasurementManager
from ooni.reporter import Report
from ooni.utils import log, pushFilenameStack
from ooni.utils.net import randomFreePort
from ooni.nettest import NetTest, getNetTestInformation
from ooni.settings import config
from ooni import errors

from txtorcon import TorConfig, TorState, launch_tor, build_tor_connection

from twisted.internet import defer, reactor
from twisted.internet.endpoints import TCP4ClientEndpoint


class Director(object):

    """
    Singleton object responsible for coordinating the Measurements Manager
    and the Reporting Manager.

    How this all looks like is as follows:

    +------------------------------------------------+
    |                   Director                     |<--+
    +------------------------------------------------+   |
        ^                                ^               |
        |        Measurement             |               |
    +---------+  [---------]    +--------------------+   |
    |         |                 | MeasurementManager |   |
    | NetTest |  [---------]    +--------------------+   |
    |         |                 | [----------------] |   |
    +---------+  [---------]    | [----------------] |   |
        |                       | [----------------] |   |
        |                       +--------------------+   |
        v                                                |
    +---------+   ReportEntry                            |
    |         |   [---------]    +--------------------+  |
    |  Report |                  | ReportEntryManager |  |
    |         |   [---------]    +--------------------+  |
    +---------+                  | [----------------] |  |
                  [---------]    | [----------------] |--
                                 | [----------------] |
                                 +--------------------+

    [------------] are Tasks

    +------+
    |      |  are TaskManagers
    +------+
    |      |
    +------+

    +------+
    |      |  are general purpose objects
    +------+

    """

    _scheduledTests = 0
    # Only list NetTests belonging to these categories
    categories = ['blocking', 'manipulation']

    def __init__(self):
        self.activeNetTests = []

        self.measurementManager = MeasurementManager()
        self.measurementManager.director = self

        self.reportEntryManager = ReportEntryManager()
        self.reportEntryManager.director = self
        # Link the TaskManager's by least available slots.
        self.measurementManager.child = self.reportEntryManager
        # Notify the parent when tasks complete # XXX deadlock!?
        self.reportEntryManager.parent = self.measurementManager

        self.successfulMeasurements = 0
        self.failedMeasurements = 0

        self.totalMeasurements = 0

        # The cumulative runtime of all the measurements
        self.totalMeasurementRuntime = 0

        self.failures = []

        self.torControlProtocol = None

        # This deferred is fired once all the measurements and their reporting
        # tasks are completed.
        self.allTestsDone = defer.Deferred()
        self.sniffer = None

    def getNetTests(self):
        nettests = {}

        def is_nettest(filename):
            return not filename == '__init__.py' \
                and filename.endswith('.py')

        for category in self.categories:
            dirname = os.path.join(config.nettest_directory, category)
            # print path to all filenames.
            for filename in os.listdir(dirname):
                if is_nettest(filename):
                    net_test_file = os.path.join(dirname, filename)
                    nettest = getNetTestInformation(net_test_file)
                    nettest['category'] = category.replace('/', '')

                    if nettest['id'] in nettests:
                        log.err("Found a two tests with the same name %s, %s" %
                                (net_test_file,
                                 nettests[nettest['id']]['path']))
                    else:
                        category = dirname.replace(config.nettest_directory,
                                                   '')
                        nettests[nettest['id']] = nettest

        return nettests

    @defer.inlineCallbacks
    def start(self, start_tor=False):
        self.netTests = self.getNetTests()

        if config.advanced.start_tor and start_tor:
            yield self.startTor()
        elif config.tor.control_port:
            log.msg("Connecting to Tor Control Port...")
            yield self.getTorState()

        if config.global_options['no-geoip']:
            aux = [False]
            if config.global_options.get('annotations') is not None:
                annotations = [k.lower() for k in config.global_options['annotations'].keys()]
                aux = map(lambda x: x in annotations, ["city", "country", "asn"])
            if not all(aux):
                log.msg("You should add annotations for the country, city and ASN")
        else:
            yield config.probe_ip.lookup()

    @property
    def measurementSuccessRatio(self):
        if self.totalMeasurements == 0:
            return 0

        return self.successfulMeasurements / self.totalMeasurements

    @property
    def measurementFailureRatio(self):
        if self.totalMeasurements == 0:
            return 0

        return self.failedMeasurements / self.totalMeasurements

    @property
    def measurementSuccessRate(self):
        """
        The speed at which tests are succeeding globally.

        This means that fast tests that perform a lot of measurements will
        impact this value quite heavily.
        """
        if self.totalMeasurementRuntime == 0:
            return 0

        return self.successfulMeasurements / self.totalMeasurementRuntime

    @property
    def measurementFailureRate(self):
        """
        The speed at which tests are failing globally.
        """
        if self.totalMeasurementRuntime == 0:
            return 0

        return self.failedMeasurements / self.totalMeasurementRuntime

    def measurementTimedOut(self, measurement):
        """
        This gets called every time a measurement times out independenty from
        the fact that it gets re-scheduled or not.
        """
        pass

    def measurementStarted(self, measurement):
        self.totalMeasurements += 1

    def measurementSucceeded(self, result, measurement):
        log.debug("Successfully completed measurement: %s" % measurement)
        self.totalMeasurementRuntime += measurement.runtime
        self.successfulMeasurements += 1
        measurement.result = result
        return measurement

    def measurementFailed(self, failure, measurement):
        log.debug("Failed doing measurement: %s" % measurement)
        self.totalMeasurementRuntime += measurement.runtime

        self.failedMeasurements += 1
        self.failures.append((failure, measurement))
        measurement.result = failure
        return measurement

    def reporterFailed(self, failure, net_test):
        """
        This gets called every time a reporter is failing and has been removed
        from the reporters of a NetTest.
        Once a report has failed to be created that net_test will never use the
        reporter again.

        XXX hook some logic here.
        note: failure contains an extra attribute called failure.reporter
        """
        pass

    def netTestDone(self, net_test):
        self.activeNetTests.remove(net_test)
        if len(self.activeNetTests) == 0:
            self.allTestsDone.callback(None)

    @defer.inlineCallbacks
    def startNetTest(self, net_test_loader, report_filename,
                     collector_address=None):
        """
        Create the Report for the NetTest and start the report NetTest.

        Args:
            net_test_loader:
                an instance of :class:ooni.nettest.NetTestLoader
        """

        if self.allTestsDone.called:
            self.allTestsDone = defer.Deferred()

        if config.privacy.includepcap:
            if not config.reports.pcap:
                config.reports.pcap = config.generate_pcap_filename(
                    net_test_loader.testDetails
                )
            self.startSniffing()

        report = Report(net_test_loader.testDetails, report_filename,
                        self.reportEntryManager, collector_address)

        net_test = NetTest(net_test_loader, report)
        net_test.director = self

        yield net_test.report.open()

        yield net_test.initializeInputProcessor()
        try:
            self.activeNetTests.append(net_test)
            self.measurementManager.schedule(net_test.generateMeasurements())

            yield net_test.done
            yield report.close()
        finally:
            self.netTestDone(net_test)

    def startSniffing(self):
        """ Start sniffing with Scapy. Exits if required privileges (root) are not
        available.
        """
        from ooni.utils.txscapy import ScapyFactory, ScapySniffer
        config.scapyFactory = ScapyFactory(config.advanced.interface)

        if os.path.exists(config.reports.pcap):
            log.msg("Report PCAP already exists with filename %s" %
                    config.reports.pcap)
            log.msg("Renaming files with such name...")
            pushFilenameStack(config.reports.pcap)

        if self.sniffer:
            config.scapyFactory.unRegisterProtocol(self.sniffer)
        self.sniffer = ScapySniffer(config.reports.pcap)
        config.scapyFactory.registerProtocol(self.sniffer)
        log.msg("Starting packet capture to: %s" % config.reports.pcap)

    @defer.inlineCallbacks
    def getTorState(self):
        connection = TCP4ClientEndpoint(reactor, '127.0.0.1',
                                        config.tor.control_port)
        config.tor_state = yield build_tor_connection(connection)

    def startTor(self):
        """ Starts Tor
        Launches a Tor with :param: socks_port :param: control_port
        :param: tor_binary set in ooniprobe.conf
        """
        log.msg("Starting Tor...")

        @defer.inlineCallbacks
        def state_complete(state):
            config.tor_state = state
            log.msg("Successfully bootstrapped Tor")
            log.debug("We now have the following circuits: ")
            for circuit in state.circuits.values():
                log.debug(" * %s" % circuit)

            socks_port = yield state.protocol.get_conf("SocksPort")
            control_port = yield state.protocol.get_conf("ControlPort")

            config.tor.socks_port = int(socks_port.values()[0])
            config.tor.control_port = int(control_port.values()[0])

        def setup_failed(failure):
            log.exception(failure)
            raise errors.UnableToStartTor

        def setup_complete(proto):
            """
            Called when we read from stdout that Tor has reached 100%.
            """
            log.debug("Building a TorState")
            config.tor.protocol = proto
            state = TorState(proto.tor_protocol)
            state.post_bootstrap.addCallback(state_complete)
            state.post_bootstrap.addErrback(setup_failed)
            return state.post_bootstrap

        def updates(prog, tag, summary):
            log.msg("%d%%: %s" % (prog, summary))

        tor_config = TorConfig()
        if config.tor.control_port:
            tor_config.ControlPort = config.tor.control_port

        if config.tor.socks_port:
            tor_config.SocksPort = config.tor.socks_port

        if config.tor.data_dir:
            data_dir = os.path.expanduser(config.tor.data_dir)

            if not os.path.exists(data_dir):
                log.msg("%s does not exist. Creating it." % data_dir)
                os.makedirs(data_dir)
            tor_config.DataDirectory = data_dir

        if config.tor.bridges:
            tor_config.UseBridges = 1
            if config.advanced.obfsproxy_binary:
                tor_config.ClientTransportPlugin = (
                    'obfs2,obfs3 exec %s managed' %
                    config.advanced.obfsproxy_binary
                )
            bridges = []
            with open(config.tor.bridges) as f:
                for bridge in f:
                    if 'obfs' in bridge:
                        if config.advanced.obfsproxy_binary:
                            bridges.append(bridge.strip())
                    else:
                        bridges.append(bridge.strip())
            tor_config.Bridge = bridges

        if config.tor.torrc:
            for i in config.tor.torrc.keys():
                setattr(tor_config, i, config.tor.torrc[i])

        tor_config.save()

        if not hasattr(tor_config, 'ControlPort'):
            control_port = int(randomFreePort())
            tor_config.ControlPort = control_port
            config.tor.control_port = control_port

        if not hasattr(tor_config, 'SocksPort'):
            socks_port = int(randomFreePort())
            tor_config.SocksPort = socks_port
            config.tor.socks_port = socks_port

        tor_config.save()
        log.debug("Setting control port as %s" % tor_config.ControlPort)
        log.debug("Setting SOCKS port as %s" % tor_config.SocksPort)

        if config.advanced.tor_binary:
            d = launch_tor(tor_config, reactor,
                           tor_binary=config.advanced.tor_binary,
                           progress_updates=updates)
        else:
            d = launch_tor(tor_config, reactor,
                           progress_updates=updates)
        d.addCallback(setup_complete)
        d.addErrback(setup_failed)
        return d
