import type { ReactNode } from "react";
import { AlertCircle, ArrowUpFromLine, LoaderCircle } from "lucide-react";

export function Panel({
  title,
  children,
  action,
  className = "",
}: {
  title?: string;
  eyebrow?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && <header className="panel-header">
        <div>
          {title && <h2>{title}</h2>}
        </div>
        {action}
      </header>}
      {children}
    </section>
  );
}
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Upload({
  label,
  accept,
  file,
  onFile,
  compact = false,
}: {
  label: string;
  accept: string;
  file: File | null;
  onFile: (file: File) => void;
  compact?: boolean;
}) {
  return (
    <label
      className={`upload ${compact ? "compact" : ""}`}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]);
      }}
    >
      <input
        type="file"
        accept={accept}
        aria-label={label}
        onChange={(e) => {
          if (e.target.files?.[0]) onFile(e.target.files[0]);
          e.target.value = "";
        }}
      />
      <ArrowUpFromLine size={compact ? 17 : 24} />
      <span>
        <strong>{file?.name ?? label}</strong>
        {(!compact || file) && <small>
          {file
            ? `${(file.size / 1024).toFixed(0)} КБ`
            : "або перетягніть файл"}
        </small>}
      </span>
    </label>
  );
}
export function ErrorMessage({ message }: { message: string }) {
  return message ? (
    <div className="error-message" role="alert">
      <AlertCircle size={18} />
      <span>{message}</span>
    </div>
  ) : null;
}
export function Busy({ text, progress }: { text: string; progress?: number }) {
  return (
    <div className="busy" role="status" aria-live="polite">
      <div>
        <LoaderCircle className="spin" size={17} />
        <span>{text}</span>
        {progress !== undefined && <strong>{progress}%</strong>}
      </div>
      {progress !== undefined && <progress value={progress} max={100} />}
    </div>
  );
}
