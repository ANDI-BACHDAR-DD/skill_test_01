from slixmpp.plugins.base import register_plugin

from . import stanza, vcard4

register_plugin(vcard4.XEP_0292)
