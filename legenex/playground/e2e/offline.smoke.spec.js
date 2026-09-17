import { expect, test } from '@playwright/test';

test('proxy health and allow-list', async ({ request }) => {
  const health = await request.get('/pg/health');
  expect(health.status()).toBe(200);
  expect((await health.json()).upstream.ok).toBe(true);
  expect((await request.get('/api/session')).status()).toBe(200);
  expect((await request.get('/api/keys')).status()).toBe(404);
  expect((await request.get('/api/resources')).status()).toBe(404);
  expect((await request.post('/api/storage/scan', { data: {} })).status()).toBe(404);
});
