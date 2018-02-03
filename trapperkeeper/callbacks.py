from datetime import timedelta
from expvar.stats import stats
import logging
from oid_translate import ObjectId

from pyasn1.codec.ber import decoder
from pyasn1.type.error import ValueConstraintError
from pysnmp.proto import api
from pysnmp.proto.error import ProtocolError

import socket
from sqlalchemy.exc import IntegrityError, InvalidRequestError, OperationalError

from trapperkeeper.dde import DdeNotification
from trapperkeeper.constants import SNMP_VERSIONS
from trapperkeeper.models import Notification
from trapperkeeper.utils import parse_time_string, send_trap_email, index_trap_to_elasticsearch


try:
    from trapperkeeper_dde_plugin import run as dde_run
except ImportError as err:
    # If no DDE plugin is found add noop to namespace.
    def dde_run(notification):
        pass


class TrapperCallback(object):
    def __init__(self, conn, template_env, config, resolver, community):
        self.conn = conn
        self.template_env = template_env
        self.config = config
        self.hostname = socket.gethostname()
        self.resolver = resolver
        self.community = community

    def __call__(self, *args, **kwargs):
        try:
            self._call(*args, **kwargs)
        # Prevent the application from crashing when callback raises
        # an exception.
        except Exception as err:
            stats.incr("callback-failure", 1)
            logging.exception("Callback Failed: %s", err)

    def _send_mail(self, handler, trap, is_duplicate):
        if is_duplicate and not handler["mail_on_duplicate"]:
            return

        mail = handler["mail"]
        if not mail:
            return

        recipients = handler["mail"].get("recipients")
        if not recipients:
            return

        subject = handler["mail"]["subject"] % {
            "trap_oid": trap.oid,
            "trap_name": ObjectId(trap.oid).name,
            "ipaddress": trap.host,
            "hostname": self.resolver.hostname_or_ip(trap.host),
        }
        ctxt = dict(trap=trap, dest_host=self.hostname)
        try:
            stats.incr("mail_sent_attempted", 1)
            send_trap_email(recipients, "trapperkeeper",
                            subject, self.template_env, ctxt)
            stats.incr("mail_sent_successful", 1)
        except socket.error as err:
            stats.incr("mail_sent_failed", 1)
            logging.warning("Failed to send e-mail for trap: %s", err)

    def _call(self, transport_dispatcher, transport_domain, transport_address, whole_msg):
        if not whole_msg:
            return

        msg_version = int(api.decodeMessageVersion(whole_msg))

        if msg_version in api.protoModules:
            proto_module = api.protoModules[msg_version]
        else:
            stats.incr("unsupported-notification", 1)
            logging.error("Unsupported SNMP version %s", msg_version)
            return

        host = transport_address[0]
        version = SNMP_VERSIONS[msg_version]

        try:
            req_msg, whole_msg = decoder.decode(whole_msg, asn1Spec=proto_module.Message(),)
        except (ProtocolError, ValueConstraintError) as err:
            stats.incr("unsupported-notification", 1)
            logging.warning("Failed to receive trap (%s) from %s: %s", version, host, err)
            return
        req_pdu = proto_module.apiMessage.getPDU(req_msg)

        community = proto_module.apiMessage.getCommunity(req_msg)
        if self.community and community != self.community:
            stats.incr("unauthenticated-notification", 1)
            logging.warning("Received trap from %s with invalid community: %s... discarding", host, community)
            return

        if not req_pdu.isSameTypeWith(proto_module.TrapPDU()):
            stats.incr("unsupported-notification", 1)
            logging.warning("Received non-trap notification from %s", host)
            return

        if msg_version not in (api.protoVersion1, api.protoVersion2c):
            stats.incr("unsupported-notification", 1)
            logging.warning("Received trap not in v1 or v2c")
            return

        trap = Notification.from_pdu(host, proto_module, version, req_pdu)
        if trap is None:
            stats.incr("unsupported-notification", 1)
            logging.warning("Invalid trap from %s: %s", host, req_pdu)
            return

        dde = DdeNotification(trap, self.config.handlers[trap.oid])
        dde_run(dde)
        handler = dde.handler

        trap.severity = handler["severity"]
        trap.manager = self.hostname

        if handler.get("expiration", None):
            expires = parse_time_string(handler["expiration"])
            expires = timedelta(**expires)
            trap.expires = trap.sent + expires

        stats.incr("traps_received", 1)
        objid = ObjectId(trap.oid)
        if handler.get("blackhole", False):
            stats.incr("traps_blackholed", 1)
            logging.debug("Blackholed %s from %s", objid.name, host)
            return

        logging.info("Trap Received (%s) from %s", objid.name, host)
        stats.incr("traps_accepted", 1)


        duplicate = False
        try:
            stats.incr("db_write_attempted", 1)
            self.conn.add(trap)
            self.conn.commit()
            stats.incr("db_write_successful", 1)
        except OperationalError as err:
            self.conn.rollback()
            logging.warning("Failed to commit: %s", err)
            stats.incr("db_write_failed", 1)
            # TODO(gary) reread config and reconnect to database
        except InvalidRequestError as err:
            # If we get into this state we should rollback any pending changes.
            stats.incr("db_write_failed", 1)
            self.conn.rollback()
            logging.warning("Bad state, rolling back transaction: %s", err)
        except IntegrityError as err:
            stats.incr("db_write_duplicate", 1)
            duplicate = True
            self.conn.rollback()
            logging.info("Duplicate Trap (%s) from %s. Likely inserted by another manager.", objid.name, host)
            logging.debug(err)

        self._send_mail(handler, trap, duplicate)

        # TODO: index_to_elastic_search
        self._index_to_elasticsearch(trap)

    def _index_to_elasticsearch(self, trap):
        subject = {
            "trap_oid": trap.oid,
            "trap_name": ObjectId(trap.oid).name,
            "ipaddress": trap.host,
            "hostname": self.resolver.hostname_or_ip(trap.host),
        }
        import pprint
        pp = pprint.PrettyPrinter(indent=4)
        print("="*150)
        pp.pprint(trap.to_dict())
        print("vb"*25)
        for vb in trap.varbinds:
            pp.pprint(vb.to_dict(pretty=True))

        trap_index = dict()
        trap_index.update(trap.to_dict())

        for vb in trap.varbinds:
            trap_index.update(transform_varbind(vb))

        trap_index["notification_id"] = trap_index["id"]
        trap_index["mib_name"] = ObjectId(trap.oid).name
        del trap_index["id"]
        print("-"*25)
        pp.pprint(trap_index)
        trap.pprint()
        index_trap_to_elasticsearch(trap_index)

        #pp.pprint(trap.varbinds)
        #check utils.to_mibname
        #trap.varbinds[0].pretty_value....
        #trap.varbinds[1].pretty_value....


def transform_varbind(varbind):
    d = varbind.to_dict(pretty=True)
    # {'name': 'SNMPv2-MIB::sysLocation.0',
    #  'notification_id': 40,
    #  'oid': u'1.3.6.1.2.1.1.6.0',
    #  'pretty_value': 'TrapperKeeper-Test',
    #  'value': 'TrapperKeeper-Test',
    #  'value_type': u'octet'}
    result = dict()
    result.update({
        d['name']: d['pretty_value'],
        d['oid']: d['value'],
        "%s:type" % (d['name']): d['value_type']
    })
    return result