import { Field } from "./components";
import { localTimestamp } from "./timestamps";
import type { TimestampConfig } from "./timestamps";
import type { Dispatch, SetStateAction } from "react";
export { lastWrittenAt, planLastWrittenAt } from "./timestamps";
export type { TimestampConfig } from "./timestamps";

export default function TimestampFields({ value, onChange }: {
  value: TimestampConfig; onChange: Dispatch<SetStateAction<TimestampConfig>>;
}) {
  return <div className="timestamp-fields">
    <label className="check-label"><input type="checkbox" checked={value.enabled}
      onChange={e => { const enabled = e.currentTarget.checked;
        onChange(previous => ({ ...previous, enabled, start: previous.start || localTimestamp() })); }} />LastWriteTime</label>
    {value.enabled && <>
      <div className="timestamp-grid">
        <Field label="Початок · Київ"><input type="datetime-local" step="1" min="1980-01-01T00:00" max="2107-12-31T23:59:59"
          value={value.start}
          onInput={e => { const start = e.currentTarget.value;
            onChange(previous => ({ ...previous, start })); }}
          onChange={e => { const start = e.currentTarget.value;
            onChange(previous => ({ ...previous, start })); }} /></Field>
        <Field label="Крок · хвилини"><input value={value.step} placeholder="6-8" inputMode="decimal"
          onChange={e => { const step = e.currentTarget.value;
            onChange(previous => ({ ...previous, step })); }} /></Field>
      </div>
      <p className="hint">Дата файлів у ZIP після розпакування. Окреме скачування — у ZIP з датою.</p>
    </>}
  </div>;
}
