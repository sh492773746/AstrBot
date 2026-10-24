import guide from "./guide-base64.txt";

const bytes = Uint8Array.from(atob(guide), (char) => char.charCodeAt(0));

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (!["/feature-guide-v1.jpg", "/feature-guide-v2.jpg"].includes(url.pathname)) {
      return new Response("Not found", { status: 404 });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { Allow: "GET, HEAD" },
      });
    }
    return new Response(request.method === "HEAD" ? null : bytes, {
      headers: {
        "Content-Type": "image/jpeg",
        "Content-Length": String(bytes.byteLength),
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
