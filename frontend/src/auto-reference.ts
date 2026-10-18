export type AutoSavedReference<T> = { template: T; created: boolean };

/** Update the catalog only; the current custom source and render state stay untouched. */
export function upsertReference<T extends { id: string }>(references: readonly T[], reference: T): T[] {
  const index = references.findIndex(item => item.id === reference.id);
  if (index < 0) return [...references, reference];
  return references.map((item, itemIndex) => itemIndex === index ? reference : item);
}

/** An older catalog response must not hide references saved while that request was in flight. */
export function catalogWithConcurrentSaves<T extends { id: string }>(
  catalog: T[],
  saved: Iterable<{ revision: number; template: T }>,
  requestRevision: number,
): T[] {
  let next = catalog;
  for (const item of saved) {
    if (item.revision > requestRevision) next = upsertReference(next, item.template);
  }
  return next;
}

/** Later colors from the same batch must not turn a newly saved notice into a duplicate notice. */
export function rememberSavedReference<T extends { id: string }>(
  previous: AutoSavedReference<T> | null,
  saved: AutoSavedReference<T>,
): AutoSavedReference<T> {
  return { ...saved, created: saved.created || (previous?.template.id === saved.template.id && previous.created) };
}

export function referenceSaveWarnings(warnings: string[], saveWarning?: string): string[] {
  return saveWarning && !warnings.includes(saveWarning) ? [...warnings, saveWarning] : warnings;
}
