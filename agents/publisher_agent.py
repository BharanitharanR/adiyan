from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any, Callable, Optional
import inspect
import pika
import json
from datetime import datetime

class PublisherAgent(BaseAgent):
    """Agent 7: Publish response back to WhatsApp and RabbitMQ"""

    def __init__(self, config: Dict[str, Any] = None, whatsapp_sender: Optional[Callable] = None):
        tools = ['send_whatsapp_reply', 'publish_event', 'log_completion']
        super().__init__('PublisherAgent', tools, config)
        self.rabbitmq_url = config.get('rabbitmq_url', 'amqp://guest:guest@localhost/') if config else 'amqp://guest:guest@localhost/'
        self.whatsapp_sender = whatsapp_sender  # Callback to send WhatsApp message

    async def execute(self, state: AgentState) -> AgentState:
        """Publish response"""
        try:
            # Handle error case
            if state.error:
                self.log_stage(f"Pipeline error: {state.error}", 'error')
                error_msg = f"❌ {state.error}"
                await self._send_response(state, error_msg)
                await self._publish_event(state, 'error')
                return state

            # Handle registration
            if state.is_registration:
                response = f"✅ Registered: {state.contact_name}\n\nYou can now ask me for coaching on productivity, goal-setting, and personal development!"
                await self._send_response(state, response)
                await self._publish_event(state, 'registered')
                self.log_stage(f"✅ Registration complete: {state.contact_name}")
                return state

            # Handle unregistration
            if state.is_unregistration:
                response = f"✅ Unregistered: {state.contact_name}"
                await self._send_response(state, response)
                await self._publish_event(state, 'unregistered')
                self.log_stage(f"✅ Unregistration complete: {state.contact_name}")
                return state

            # Send coaching response (join chunks)
            if state.metadata.get('chunks'):
                chunks = state.metadata['chunks']
                full_response = '\n'.join(chunks)
                await self._send_response(state, full_response)
                await self._publish_event(state, 'response_sent')
                self.log_stage(f"✅ Response sent ({len(chunks)} chunk(s))")
                return state

            # No response to send
            self.log_stage(f"⚠️  No response to send", 'warning')
            return state

        except Exception as e:
            self.log_stage(f"❌ Publish failed: {str(e)}", 'error')
            return state

    async def _send_response(self, state: AgentState, response: str):
        """Send response back to WhatsApp, preferring the injected sender (e.g. OpenWA)
        over the legacy RabbitMQ queue, which nothing may be consuming anymore."""
        if self.whatsapp_sender:
            try:
                if inspect.iscoroutinefunction(self.whatsapp_sender):
                    await self.whatsapp_sender(state.lid, response)
                else:
                    self.whatsapp_sender(state.lid, response)
                self.log_stage(f"📤 Response sent directly to {state.contact_name}")
                return
            except Exception as e:
                self.log_stage(f"❌ Direct send failed, falling back to queue: {str(e)}", 'error')

        try:
            connection = pika.BlockingConnection(
                pika.URLParameters(self.rabbitmq_url)
            )
            channel = connection.channel()
            # Don't declare queue - Node.js already created it
            # Just publish to it directly

            response_msg = {
                'message_id': state.message_id,
                'contact_name': state.contact_name,
                'response': response
            }

            channel.basic_publish(
                exchange='',  # Use default exchange
                routing_key='whatsapp-responses',  # Queue name is the routing key
                body=json.dumps(response_msg),
                properties=pika.BasicProperties(delivery_mode=2)
            )

            connection.close()
            self.log_stage(f"📤 Response queued for {state.contact_name}")

        except Exception as e:
            self.log_stage(f"❌ Failed to queue response: {str(e)}", 'error')

    async def _publish_event(self, state: AgentState, event_type: str):
        """Publish event to RabbitMQ and send response back to WhatsApp"""
        try:
            connection = pika.BlockingConnection(
                pika.URLParameters(self.rabbitmq_url)
            )
            channel = connection.channel()
            channel.exchange_declare(exchange='adiyan', exchange_type='topic', durable=True)
            channel.queue_declare(queue='whatsapp-responses', durable=True)

            event = {
                'timestamp': datetime.now().isoformat(),
                'event_type': event_type,
                'contact_name': state.contact_name,
                'message_id': state.message_id,
                'persona': state.persona,
                'metadata': state.metadata
            }

            channel.basic_publish(
                exchange='adiyan',
                routing_key=f'events.{event_type}',
                body=json.dumps(event),
                properties=pika.BasicProperties(delivery_mode=2)
            )

            connection.close()
            self.log_stage(f"📤 Event published: {event_type}", 'debug')

        except Exception as e:
            self.log_stage(f"⚠️  RabbitMQ publish failed: {str(e)}", 'warning')
