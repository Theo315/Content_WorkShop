type WaveRoot = {
  dataset: { themeTransition?: string };
  style: { setProperty(name: string, value: string): void; removeProperty(name: string): string };
};
const waveProperties = ["--theme-wave-x", "--theme-wave-y", "--theme-wave-radius"] as const;

export function prepareThemeWave(root: WaveRoot, originX: number, originY: number, width: number, height: number) {
  const x = Math.min(width, Math.max(0, originX));
  const y = Math.min(height, Math.max(0, originY));
  const radius = Math.hypot(Math.max(x, width - x), Math.max(y, height - y)) + 2;
  // Set geometry before startViewTransition: its new snapshot must already
  // have a running CSS animation when the browser creates the pseudo-tree.
  [x, y, radius].forEach((value, index) => root.style.setProperty(waveProperties[index], `${value}px`));
  root.dataset.themeTransition = "wave";
}

export function clearThemeWave(root: WaveRoot) {
  delete root.dataset.themeTransition;
  waveProperties.forEach(property => root.style.removeProperty(property));
}
