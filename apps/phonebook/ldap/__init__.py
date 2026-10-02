"""Read-only LDAP v3 directory server for the event phonebook (desk phones, Mitel OMM).

Layers: :mod:`ber` (ASN.1 BER codec) -> :mod:`protocol` (LDAP PDUs + filters) -> :mod:`directory`
(entries from :mod:`apps.phonebook.services`) -> :mod:`server` (asyncio TCP/TLS server).
Started with ``manage.py dial_ldap``.
"""
