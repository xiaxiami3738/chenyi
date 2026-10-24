# 站点点缀素材说明（M9）

`app/static/img/` 下的 4 张鲸鱼娘点缀图由 AI 生成，仅用于本项目的界面装饰，不参与任何功能逻辑。

## 生成方式

- 供应商：阿里云百炼 DashScope（本机 DSH 里配置的 `yuan` / provider id `aliyun2`）
- 模型：`qwen-image-3.0-pro`（多模态生图接口 `POST /api/v1/services/aigc/multimodal-generation/generation`）
- 参数：`size = 1024*1024`、`n = 1`、`watermark = false`，白色背景、扁平插画风格、蓝色调

## 文件对应

| 文件 | 用途 | 提示词要点 |
| --- | --- | --- |
| `whale-hero.png` | 首页 hero 右侧点缀 | Q 版蓝发鲸鱼娘，白色背景，扁平插画，简洁 |
| `whale-side.png` | 备用主视觉（暂未引用） | 鲸鱼外壳 + 少女脸的 Q 版形象 |
| `whale-empty.png` | 首页本地问答「没找到结果」空态小图 | 小尺寸 Q 版鲸鱼，白底 |
| `whale-pattern.png` | 页面右下角水印底纹（`body::before`，透明度 0.055，`mix-blend-mode: multiply`） | 浅蓝鲸尾与浪花组成的无缝平铺图形 |

## 使用约定

- 全部是本地文件，不引用 CDN、不请求外部字体，断网也能正常显示。
- 装饰图一律 `aria-hidden="true"`（空 `alt`），不干扰读屏软件。
- 移动端（≤680px）隐藏 hero 装饰图，避免挤压正文。
- 若要替换素材，保持同样的白底 / 蓝色调 / 扁平风格，覆盖同名文件即可，无需改代码。