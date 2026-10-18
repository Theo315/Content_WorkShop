export async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? body?.error;
    throw new Error(
      typeof detail === "string"
        ? detail
        : response.status === 401
          ? "Потрібен вхід. Перевірте пароль доступу."
          : `Помилка сервера (${response.status}). Спробуйте ще раз.`,
    );
  }
  return response.json() as Promise<T>;
}
export function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
export function readDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("Не вдалося прочитати файл."));
    reader.readAsDataURL(file);
  });
}
export type FileRef = {
  token: string;
  name: string;
  url: string;
  size: number;
};
export async function uploadFile(
  file: File | Blob,
  filename?: string,
): Promise<FileRef> {
  const info = await request<{
    upload_url: string;
    method: string;
    headers: Record<string, string>;
    file: FileRef;
  }>("/api/files/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: filename ?? (file as File).name,
      content_type: file.type || "application/octet-stream",
      size: file.size,
    }),
  });
  const response = await fetch(info.upload_url, {
    method: info.method,
    headers: info.headers,
    body: file,
  });
  if (!response.ok)
    throw new Error(`Не вдалося завантажити файл (${response.status}).`);
  return info.file;
}
export function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
export async function downloadFile(file: FileRef) {
  download(await fetchFile(file), file.name);
}

export async function refreshFile(file: FileRef): Promise<FileRef> {
  return (
    await post<{ file: FileRef }>("/api/files/refresh", { token: file.token })
  ).file;
}

export async function fetchFile(file: FileRef): Promise<Blob> {
  const fresh = await refreshFile(file);
  const response = await fetch(fresh.url);
  if (!response.ok)
    throw new Error("Файл недоступний. Посилання могло завершити строк дії.");
  return response.blob();
}
