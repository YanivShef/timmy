"""External services (build step 4, done).

secrets.py     Windows Credential Manager via keyring, values chunked under the 2560-byte blob limit
greenolink.py  OAuth 2.1 + PKCE loopback login and MCP over Streamable HTTP (search_contacts, send_message,
               request_file_upload, send_file). Tests run only against tests/mock_greenolink.py.
whatsapp.py    the send flow: draft -> review -> confirm -> send (direct, or through Claude Code's Greenolink MCP)
"""
