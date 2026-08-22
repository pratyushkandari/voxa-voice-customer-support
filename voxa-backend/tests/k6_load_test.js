import ws from 'k6/ws';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 50 },  // Ramp up to 50 users
    { duration: '1m', target: 200 },  // Ramp up to 200 users
    { duration: '1m', target: 500 },  // Peak load: 500 concurrent users
    { duration: '30s', target: 0 },   // Ramp down
  ],
  thresholds: {
    'ws_connecting': ['p(95)<200'], // 95% of connections establish in <200ms
  },
};

export default function () {
  const url = 'ws://localhost:8000/v1/ws/stream';
  const params = { tags: { my_tag: 'voxa_ws_client' } };

  const res = ws.connect(url, params, function (socket) {
    socket.on('open', () => {
      // Send text query
      socket.send(JSON.stringify({
        type: 'text_query',
        text: 'What is the refund policy for order ORD-88412?',
        trace_id: `k6-trace-${Date.now()}`
      }));
    });

    socket.on('message', (data) => {
      const msg = JSON.parse(data);
      if (msg.type === 'token_chunk' && msg.is_final) {
        check(msg, {
          'TTFT is sub-200ms': (m) => m.ttft_ms < 200,
        });
        socket.close();
      }
    });

    socket.on('close', () => {});
  });

  check(res, { 'WebSocket connection successful': (r) => r && r.status === 101 });
  sleep(1);
}
