// Cloudflare Pages / Workers kenar vekili (müşteri trafiği -> kaynak sunucu).
// Cloudflare panosunda (Variables and Secrets) tanımlanabilecek ortam değişkenleri:
//   UPSTREAM_ORIGIN    : zorunlu — kaynak sunucu kökeni (örn. https://sunucunuz.duckdns.org)
//   UPSTREAM_PREFIX    : isteğe bağlı — sunucudaki Caddy alt yolu (örn. "/k", kök yayın için boş "")
//   PAGES_PROJECT_HOST : isteğe bağlı — Cloudflare Pages proje adresi (örn. kafem.pages.dev); önizleme alt alanlarını da açar
//   ALLOWED_HOSTS      : isteğe bağlı — virgülle ayrılmış özel alan adları (örn. kafem.com,www.kafem.com)
//   ALLOWED_HOST       : isteğe bağlı — tek ek alan adı (geriye dönük uyumluluk için)
//   EDGE_SHARED_SECRET : önerilen — sunucudaki .env içindeki EDGE_SHARED_SECRET ile AYNI değer (Secret)
//   GOOGLE_REVIEW_URL  : isteğe bağlı — /yorum adresi için Google yorum bağlantısı

const STRIP_EXACT = new Set(["x-forwarded-for", "x-real-ip", "forwarded", "x-client-ip", "true-client-ip"]);

function parseAllowedHosts(env) {
  const hosts = new Set();
  const rawList = [env && env.ALLOWED_HOSTS, env && env.ALLOWED_HOST].filter(Boolean).join(",");
  for (const item of rawList.split(",")) {
    const clean = item.trim().toLowerCase();
    if (clean) hosts.add(clean);
  }
  return hosts;
}

function isAllowedHost(hostname, env) {
  const host = String(hostname || "").toLowerCase();
  if (!host) return false;
  const pagesHost = String((env && env.PAGES_PROJECT_HOST) || "").trim().toLowerCase();
  const customHosts = parseAllowedHosts(env);
  if (pagesHost && (host === pagesHost || host.endsWith("." + pagesHost))) return true;
  if (customHosts.has(host)) return true;
  // Açık host listesi verilmediyse Cloudflare Pages önizleme/üretim alt alanlarını kabul et
  return !pagesHost && customHosts.size === 0 && host.endsWith(".pages.dev");
}

function resolveUpstreamOrigin(env) {
  const raw = String((env && env.UPSTREAM_ORIGIN) || "").trim().replace(/\/+$/, "");
  if (!raw || (!raw.startsWith("https://") && !raw.startsWith("http://"))) return "";
  return raw;
}

export async function onRequest(context) {
  const { request, env } = context;
  const incomingUrl = new URL(request.url);

  if (!isAllowedHost(incomingUrl.hostname, env)) {
    return new Response("Not Found", { status: 404, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }

  const upstreamOrigin = resolveUpstreamOrigin(env);
  if (!upstreamOrigin) {
    return new Response("Edge proxy UPSTREAM_ORIGIN env is not configured.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  const reviewUrl = String((env && env.GOOGLE_REVIEW_URL) || "").trim();
  const cleanPath = incomingUrl.pathname.replace(/\/+$/, "") || "/";
  if (reviewUrl && (cleanPath === "/yorum" || cleanPath === "/k/yorum")) {
    return Response.redirect(reviewUrl, 302);
  }

  const rawPrefix = String((env && env.UPSTREAM_PREFIX) || "").trim().replace(/^\/+|\/+$/g, "");
  const upstreamPrefix = rawPrefix ? `/${rawPrefix}` : "";

  let targetPath = incomingUrl.pathname;
  if (upstreamPrefix) {
    if (targetPath === "/" || targetPath === "") {
      targetPath = upstreamPrefix + "/";
    } else if (targetPath !== upstreamPrefix && !targetPath.startsWith(upstreamPrefix + "/")) {
      targetPath = upstreamPrefix + targetPath;
    }
  }
  const upstreamUrl = new URL(targetPath + incomingUrl.search, upstreamOrigin);

  const reqHeaders = new Headers(request.headers);
  for (const name of [...reqHeaders.keys()]) {
    const lower = name.toLowerCase();
    if (STRIP_EXACT.has(lower) || lower.startsWith("x-edge-")) reqHeaders.delete(name);
  }
  reqHeaders.delete("host");
  reqHeaders.set("X-Forwarded-Host", incomingUrl.host);
  reqHeaders.set("X-Edge-Host", incomingUrl.host);
  reqHeaders.set("X-Forwarded-Proto", incomingUrl.protocol.replace(":", "") || "https");

  const clientIp = request.headers.get("CF-Connecting-IP");
  if (clientIp) {
    reqHeaders.set("X-Edge-Client-IP", clientIp);
  }
  const secret = String((env && env.EDGE_SHARED_SECRET) || "");
  if (secret) {
    reqHeaders.set("X-Edge-Auth", secret);
  }

  const isHead = request.method === "HEAD";
  const init = {
    method: isHead ? "GET" : request.method,
    headers: reqHeaders,
    redirect: "manual",
    cf: { cacheEverything: false, cacheTtlByStatus: { "100-599": -1 } },
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
  }

  const upstreamResponse = await fetch(upstreamUrl.toString(), init);

  const respHeaders = new Headers(upstreamResponse.headers);
  respHeaders.delete("content-encoding");
  respHeaders.delete("content-length");

  return new Response(isHead ? null : upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: respHeaders,
  });
}

export default {
  async fetch(request, env, ctx) {
    return onRequest({ request, env, ctx });
  },
};
