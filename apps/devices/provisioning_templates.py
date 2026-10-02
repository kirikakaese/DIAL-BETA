"""Built-in autoprovisioning templates for common hard phones.

Loaded as global (``event=None``) ``ProvisioningProfile`` rows by ``manage.py pet_provisioning_profiles``.
Template context (see ``ProvisioningProfile.render``): ``device``, ``extension``, ``event``,
``sip_server``, ``sip_port``, ``transport``, ``display_name``, ``phonebook_url`` (remote phonebook served under
the device's own provisioning token; empty when the event's remote directory is disabled).
"""
from __future__ import annotations

from .models import ProvisioningProfile

V = ProvisioningProfile.Vendor

SNOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<settings>
  <phone-settings>
    <user_active idx="1" perm="">on</user_active>
    <user_realname idx="1" perm="">{{ display_name }}</user_realname>
    <user_name idx="1" perm="">{{ device.sip_username }}</user_name>
    <user_pname idx="1" perm="">{{ device.sip_username }}</user_pname>
    <user_pass idx="1" perm="">{{ device.sip_password }}</user_pass>
    <user_host idx="1" perm="">{{ sip_server }}</user_host>
    <user_outbound idx="1" perm="">{{ sip_server }}:{{ sip_port }};transport={{ transport }}</user_outbound>
    <user_srtp idx="1" perm="">{% if transport == "tls" %}on{% else %}off{% endif %}</user_srtp>
{% if phonebook_url %}    <dkey_directory perm="">url {{ phonebook_url }}</dkey_directory>
{% endif %}  </phone-settings>
</settings>
"""

YEALINK_CFG = """{% autoescape off %}#!version:1.0.0.1
account.1.enable = 1
account.1.label = {{ display_name }}
account.1.display_name = {{ display_name }}
account.1.auth_name = {{ device.sip_username }}
account.1.user_name = {{ device.sip_username }}
account.1.password = {{ device.sip_password }}
account.1.sip_server.1.address = {{ sip_server }}
account.1.sip_server.1.port = {{ sip_port }}
account.1.sip_server.1.transport_type = {% if transport == "tls" %}2{% elif transport == "tcp" %}1{% else %}0{% endif %}
{% if phonebook_url %}features.remote_phonebook.enable = 1
remote_phonebook.data.1.url = {{ phonebook_url }}
remote_phonebook.data.1.name = {{ event.name }}
{% endif %}{% endautoescape %}"""

# Grandstream XML config with P-values: P270 account name, P47 SIP server, P35 user ID, P36 auth ID,
# P34 password, P3 display name, P271 account active, P130 transport (0 UDP, 1 TCP, 2 TLS).
# Phonebook: P330 XML phonebook download (0 off, 1 HTTP, 3 HTTPS), P331 server path *without* scheme and
# file name (the phone appends ``/phonebook.xml`` itself), P332 download interval in minutes.
GRANDSTREAM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gs_provision version="1">
  <config version="1">
    <P271>1</P271>
    <P270>{{ display_name }}</P270>
    <P3>{{ display_name }}</P3>
    <P47>{{ sip_server }}:{{ sip_port }}</P47>
    <P35>{{ device.sip_username }}</P35>
    <P36>{{ device.sip_username }}</P36>
    <P34>{{ device.sip_password }}</P34>
    <P130>{% if transport == "tls" %}2{% elif transport == "tcp" %}1{% else %}0{% endif %}</P130>
{% if phonebook_url %}    <P330>{% if phonebook_url|slice:":6" == "https:" %}3{% else %}1{% endif %}</P330>
    <P331>{{ phonebook_url|cut:"https://"|cut:"http://"|cut:"/phonebook.xml" }}</P331>
    <P332>60</P332>
{% endif %}  </config>
</gs_provision>
"""

# Cisco SPA flat profile. The XML directory service keys are ``XML_Directory_Service_Name/_URL``
# (``<directoryURL>`` is the 79xx/88xx SEP<MAC>.cnf.xml key, not a flat-profile parameter).
CISCO_SPA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<flat-profile>
  <Line_Enable_1_>Yes</Line_Enable_1_>
  <Display_Name_1_>{{ display_name }}</Display_Name_1_>
  <User_ID_1_>{{ device.sip_username }}</User_ID_1_>
  <Auth_ID_1_>{{ device.sip_username }}</Auth_ID_1_>
  <Use_Auth_ID_1_>Yes</Use_Auth_ID_1_>
  <Password_1_>{{ device.sip_password }}</Password_1_>
  <Proxy_1_>{{ sip_server }}:{{ sip_port }}</Proxy_1_>
  <Register_1_>Yes</Register_1_>
  <SIP_Transport_1_>{{ transport|upper }}</SIP_Transport_1_>
{% if phonebook_url %}  <XML_Directory_Service_Name>{{ event.name }}</XML_Directory_Service_Name>
  <XML_Directory_Service_URL>{{ phonebook_url }}</XML_Directory_Service_URL>
{% endif %}</flat-profile>
"""

# name -> (vendor, template, content_type, filename_pattern)
BUILTIN_PROFILES: dict[str, tuple[str, str, str, str]] = {
    "Snom (built-in)": (V.SNOM, SNOM_XML, "text/xml", "{mac}.xml"),
    "Yealink (built-in)": (V.YEALINK, YEALINK_CFG, "text/plain", "{mac}.cfg"),
    "Grandstream (built-in)": (V.GRANDSTREAM, GRANDSTREAM_XML, "text/xml", "cfg{mac}.xml"),
    "Cisco SPA (built-in)": (V.CISCO, CISCO_SPA_XML, "text/xml", "spa{mac}.cfg"),
}


def load_builtin_profiles(update: bool = False) -> int:
    """Create missing global profiles; returns how many were created.

    Existing names are skipped (operators may have tuned them) unless ``update`` is set, which rewrites
    template / content type / file name of the built-in rows to the current shipped version.
    """
    created = 0
    for name, (vendor, template, content_type, pattern) in BUILTIN_PROFILES.items():
        existing = ProvisioningProfile.objects.filter(name=name, event=None).first()
        if existing is not None:
            if update:
                existing.vendor, existing.template = vendor, template
                existing.content_type, existing.filename_pattern = content_type, pattern
                existing.save(update_fields=["vendor", "template", "content_type", "filename_pattern",
                                             "updated_at"])
            continue
        ProvisioningProfile.objects.create(event=None, name=name, vendor=vendor, template=template,
                                           content_type=content_type, filename_pattern=pattern)
        created += 1
    return created
