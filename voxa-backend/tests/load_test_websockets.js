// k6 WebSocket load test for the gateway's /v1/ws/stream endpoint.
//
//   k6 run -e WS_URL=ws://localhost:8088/v1/ws/stream tests/load_test_websockets.js
//
// Note: the gateway rate-limits text queries per user / client IP
// (RATE_LIMIT_REQUESTS_PER_MINUTE, default 30). When every virtual user comes from one
// machine, raise that limit on the gateway under test or most queries get an "error" frame.
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
  const url = __ENV.WS_URL || 'ws://localhost:8088/v1/ws/stream';
  const params = { tags: { my_tag: 'voxa_ws_client' } };

  const res = ws.connect(url, params, function (socket) {
    socket.on('open', () => {
      socket.send(JSON.stringify({
        type: 'text_query',
        text: 'What is the refund policy for order ORD-88412?',
        trace_id: `k6-trace-${__VU}-${__ITER}`
      }));
    });

    socket.on('message', (data) => {
      const msg = JSON.parse(data);
      if (msg.type === 'token_chunk' && msg.is_final) {
        check(msg, {
          'TTFT is sub-200ms': (m) => m.ttft_ms < 200,
        });
        socket.close();
      } else if (msg.type === 'error') {
        check(msg, { 'no error frame': () => false });
        socket.close();
      }
    });

    // Never let a VU hang forever if no final frame arrives
    socket.setTimeout(() => socket.close(), 15000);
  });

  check(res, { 'WebSocket connection successful': (r) => r && r.status === 101 });
  sleep(1);
}
