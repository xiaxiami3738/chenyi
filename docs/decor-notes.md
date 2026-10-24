# 站点点缀素材说明（M13 更新）

`app/static/img/` 下现在只有 3 张图，全部是「鲸小鱼」角色的 AI 生成素材，仅用于界面装饰，不参与任何功能逻辑。

## 生成方式

- 供应商：阿里云百炼 DashScope（本机 DSH 里配置的 `yuan` / provider id `aliyun2`）
- 模型：`qwen-image-3.0-pro`（多模态生图接口 `POST /api/v1/services/aigc/multimodal-generation/generation`）
- 参数：`size = 1024*1024`（立绘用 `1024*1408`）、`n = 1`、`watermark = false`
- M13 起支持**带参考图生成**：请求 `content` 里同时给 `{"image": "data:image/jpeg;base64,..."}` 和文本指令，
  用来让角色设计贴合用户给的原图（深蓝长发、鲸鱼鳍耳、深海军蓝女仆装、手捧白米饭、兴奋表情）。
- 抠白底：`D:/练习/777/cutout.py` 的做法是「从四边漫水填充近白 + 浅中性灰（挖掉脚下阴影）→
  羽化 1.2px → 按 `C = (观测 - (1-a)*255) / a` 反解边缘真实颜色去白边 → 裁边 → 缩放 → 量化压体积」。

## 文件对应

| 文件 | 尺寸 / 体积 | 用途 | 说明 |
| --- | --- | --- | --- |
| `whale-xiaoyu-search-q3.png` | 448×599，59 KB（256 色 PNG，透明底） | 首页搜索框右侧小立绘（CSS 宽 112px） | 全身像，鲸鳍耳、深蓝女仆装、手捧饭碗、兴奋笑，白底抠图后边缘无反白 |
| `whale-xiaoyu-empty.png` | 288×288，23 KB（透明底） | 本地检索「没找到结果」空态小图（约 72px） | 半身胸像、挥手、张嘴笑 |
| `whale-pattern.png` | 1024×1024，12 KB（P 模式） | 页面右下角底纹（`body::before`，`opacity: .055`，`mix-blend-mode: multiply`） | 底色 #eef4ff，鲸尾鳍/星星/米粒/饭团/气泡，稀疏留白，240px 平铺无缝 |

## 使用约定

- 全部是本地文件，不引用 CDN、不请求外部字体，断网也能正常显示。
- 装饰图一律 `aria-hidden="true"`（空 `alt`），不干扰读屏软件。
- 搜索框立绘在 ≤900px 隐藏，避免挤压正文。
- 静态资源带 `?v={{version}}`（HTML 里的 CSS/JS）或 `Cache-Control: must-revalidate`（图片），
  换图覆盖同名文件即可，浏览器会在下次刷新时拿到新图，无需改代码。