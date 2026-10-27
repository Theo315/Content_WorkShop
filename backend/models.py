from datetime import datetime
from pathlib import PurePosixPath
import re
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=120_000, allow_inf_nan=False)

Short = Annotated[str, Field(max_length=180)]
Token = Annotated[str, Field(max_length=4096)]
Byte = Annotated[int, Field(ge=0, le=255)]
RGB = tuple[Byte, Byte, Byte]
Point = tuple[int, int]
Box = tuple[int, int, int, int]

class Login(Model):
    password: str = Field(max_length=1000)

class Upload(Model):
    filename: Short
    content_type: Literal["image/png", "application/pdf", "application/octet-stream"]
    size: int = Field(gt=0, le=25 * 1024 * 1024)

class FileToken(Model):
    token: Token

class Colors(Model):
    text: str = Field(max_length=50_000)
    format: Literal["list", "csv"] = "list"

class Color(Model):
    code: str = Field(min_length=1, max_length=80)
    hex: str = Field(min_length=3, max_length=7)
    metallic: bool = False

class TemplateConfig(Model):
    brand_name: str = Field(default="", max_length=120)
    plate_text_mode: Literal["brand_code", "code", "brand", "custom", "blank"] = "brand_code"
    plate_text: str = Field(default="", max_length=200)
    model_name: Short | None = None
    plate_box: Box | None = None
    key_color: str | None = Field(default=None, max_length=7)
    font_size: int = Field(default=34, ge=6, le=300)
    auto_font_size: bool = True
    text_color: RGB = (20, 20, 20)
    plate_bg_color: RGB | Literal["auto"] | None = None

class TemplateInput(Model):
    template_id: Short | None = None
    template_token: Token | None = None

class DefaultTitle(Model):
    title_template: str = Field(min_length=1, max_length=2000)

class TemplateKey(Model):
    key_color: str | None = Field(default=None, max_length=7)

class SavedTemplate(Model):
    name: str = Field(min_length=1, max_length=180)
    image_token: Token
    car_mask_token: Token | None = None
    marker_mask_token: Token | None = None
    config: TemplateConfig

class BatchMetadata(Model):
    batch_id: str | None = Field(default=None, max_length=36)
    batch_index: int | None = Field(default=None, ge=0, le=199)
    batch_size: int | None = Field(default=None, ge=1, le=200)
    zip_path: str | None = Field(default=None, min_length=1, max_length=1000)
    last_written_at: str | None = Field(default=None, max_length=40)

    @field_validator("batch_id")
    @classmethod
    def valid_batch_id(cls, value):
        if value is None:
            return None
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError("Потрібен UUID пакета.") from None

    @field_validator("zip_path")
    @classmethod
    def safe_zip_path(cls, value):
        if value is None:
            return None
        path = PurePosixPath(value)
        if (path.is_absolute() or "\\" in value or ".." in path.parts
                or any(part in ("", ".") for part in value.split("/"))
                or re.search(r'[<>:"|?*\x00-\x1f]', value)):
            raise ValueError("Потрібен безпечний відносний шлях у ZIP.")
        return value

    @field_validator("last_written_at")
    @classmethod
    def valid_written_time(cls, value):
        if value is None:
            return None
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Потрібна ISO-дата з часовим поясом.") from None
        if date.tzinfo is None or not 1980 <= date.year <= 2107:
            raise ValueError("Дата має містити часовий пояс і бути в межах 1980–2107 (ZIP).")
        return date.isoformat()

    @model_validator(mode="after")
    def valid_batch_position(self):
        if self.batch_id is None and (self.batch_index is not None or self.batch_size is not None):
            raise ValueError("Для позиції у пакеті потрібен batch_id.")
        if self.batch_id is not None:
            self.batch_index = 0 if self.batch_index is None else self.batch_index
            self.batch_size = 1 if self.batch_size is None else self.batch_size
            if self.batch_index >= self.batch_size:
                raise ValueError("Позиція має бути меншою за кількість файлів пакета.")
        return self

    def result_metadata(self):
        return {name: getattr(self, name) for name in BatchMetadata.model_fields
                if getattr(self, name) is not None}


class PngRender(TemplateInput, BatchMetadata):
    car_mask_token: Token | None = None
    marker_mask_token: Token | None = None
    config: TemplateConfig | None = None
    color: Color
    lightness_mode: Literal["match", "keep"] = "match"
    feather: float = Field(default=1.2, ge=0, le=10)
    draw_plate: bool = True

class Mask(TemplateInput):
    kind: Literal["car", "marker"] = "car"
    backend: Literal["flood", "grabcut", "chroma"] = "flood"
    key_color: str | None = Field(default=None, max_length=7)
    positives: list[Point] = Field(default_factory=list, max_length=100)
    negatives: list[Point] = Field(default_factory=list, max_length=100)
    rects: list[Box] = Field(default_factory=list, max_length=20)
    tolerance: int = Field(default=15, ge=1, le=80)

class SDSParse(Model):
    mode: Literal["template", "exact"]
    brand: Short = ""
    title_template: str = Field(default="", max_length=2000)
    values_text: str = Field(default="", max_length=50000)
    exact_text: str = Field(default="", max_length=120000)

class SDSItem(Model):
    code: Short
    brand: Short
    color: Short
    title: str = Field(min_length=1, max_length=500)
    filename: str = Field(default="", max_length=1000)

class SDSGenerate(BatchMetadata):
    item: SDSItem
    reference_token: Token | None = None
    render_previews: bool = True
    preview_count: Literal[1, 4] = 1

class Find(Model):
    file_token: Token
    search_text: str = Field(min_length=1, max_length=1000)
    pages_spec: str = Field(default="all", max_length=500)
    exact_case: bool = True
    replace_all: bool = True

class Edit(Find, BatchMetadata):
    replacement: str = Field(max_length=2000)
