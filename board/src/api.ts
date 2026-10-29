export async function request(path: string, options: RequestInit = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let body: { detail?: unknown } | null = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      throw new Error(text.replace(/\s+/g, " ").slice(0, 180) || "请求没有成功");
    }
  }
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : "请求没有成功");
  }
  return body;
}
