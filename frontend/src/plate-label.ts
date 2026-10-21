export type PlateTextMode = "brand_code" | "code" | "brand" | "custom" | "blank";

export type PlateLabelConfig = {
  brand_name: string;
  plate_text_mode?: PlateTextMode;
  plate_text?: string;
};

// Keep this composition in sync with backend.png.plate_text.
export function plateLabel(config: PlateLabelConfig, code: string): string {
  let text: string;
  switch (config.plate_text_mode ?? "brand_code") {
    case "code": text = code; break;
    case "brand": text = config.brand_name; break;
    case "custom": text = config.plate_text ?? ""; break;
    case "blank": text = ""; break;
    default: text = `${config.brand_name} ${code}`;
  }
  return text.replace(/\s+/g, " ").trim();
}
