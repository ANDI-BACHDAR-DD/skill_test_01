import asyncio
import slixmpp
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

class TestClient(slixmpp.ClientXMPP):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        self.add_event_handler("session_start", self.on_session_start)
        self.add_event_handler("failed_auth", self.on_failed_auth)

    async def on_session_start(self, event):
        print("\n[DIAGNOSTIC] SUCCESS: Authenticated and session started successfully!")
        self.disconnect()

    def on_failed_auth(self, event):
        print("\n[DIAGNOSTIC] FAILED: Authentication failed for this account!")
        self.disconnect()

async def main():
    client = TestClient("esp32node@localhost", "esp32password")
    client.use_encryption = False
    client['feature_mechanisms'].unencrypted_plain = True
    client.connect(address=("10.10.0.167", 5222))
    await client.disconnected

if __name__ == "__main__":
    asyncio.run(main())
