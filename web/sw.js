/* 极简 service worker：只为让站点可"添加到主屏"独立窗口启动。
   刻意不做离线缓存——避免缓存旧版前端导致更新不生效。
   纯 passthrough：所有请求直接走网络。 */
self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(self.clients.claim()); });
// 不拦截 fetch（不注册 fetch handler 即为纯网络）。
