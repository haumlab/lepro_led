import asyncio
import logging
import json
from aiomqtt import Client, MqttError

_LOGGER = logging.getLogger(__name__)

class LeproMQTTClient:
    def __init__(self, host, port, ssl_context, client_id):
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self._client_id = client_id

        self._client = None
        self._loop_task = None
        self._publish_task = None
        self._message_callback = None
        self._subscriptions = set()
        self._pending_messages = asyncio.Queue()
        self._connected_event = asyncio.Event()
        self._stop_event = asyncio.Event()

    def set_message_callback(self, callback):
        self._message_callback = callback

    async def connect(self):
        if self._loop_task and not self._loop_task.done():
            return
        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._run_loop())
        self._publish_task = asyncio.create_task(self._process_pending_messages())

    async def disconnect(self):
        self._stop_event.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        if self._publish_task:
            self._publish_task.cancel()
            try:
                await self._publish_task
            except asyncio.CancelledError:
                pass
            self._publish_task = None

    async def subscribe(self, topic):
        self._subscriptions.add(topic)
        if self._connected_event.is_set() and self._client:
            try:
                await self._client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)
            except MqttError as e:
                _LOGGER.error("Failed to subscribe to %s: %s", topic, e)

    async def publish(self, topic, payload):
        await self._pending_messages.put((topic, payload))

    async def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                async with Client(
                    hostname=self._host,
                    port=self._port,
                    identifier=self._client_id,
                    tls_context=self._ssl_context,
                    clean_session=True
                ) as client:
                    self._client = client
                    self._connected_event.set()
                    _LOGGER.info("Connected to Lepro MQTT broker")

                    # Re-subscribe
                    for topic in self._subscriptions:
                        try:
                            await client.subscribe(topic)
                            _LOGGER.debug("Resubscribed to %s", topic)
                        except Exception as e:
                             _LOGGER.error("Error resubscribing to %s: %s", topic, e)

                    try:
                        async for message in client.messages:
                            if self._message_callback:
                                try:
                                    await self._message_callback(message)
                                except Exception as e:
                                    _LOGGER.error("Error in message callback: %s", e)
                    except MqttError as e:
                         _LOGGER.warning("MQTT connection error: %s", e)
                    finally:
                         self._connected_event.clear()
                         self._client = None

            except MqttError as e:
                _LOGGER.error("Failed to connect to MQTT broker: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.exception("Unexpected error in MQTT loop: %s", e)

            if not self._stop_event.is_set():
                _LOGGER.info("Reconnecting to MQTT in 5 seconds...")
                await asyncio.sleep(5)

    async def _process_pending_messages(self):
        while True:
            try:
                topic, payload = await self._pending_messages.get()
                await self._connected_event.wait()
                if self._client:
                     await self._client.publish(topic, payload)
                     _LOGGER.debug("Published to %s", topic)
                self._pending_messages.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                 _LOGGER.error("Error publishing message: %s", e)
                 self._pending_messages.task_done()
