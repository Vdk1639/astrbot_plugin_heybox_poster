# 第三方声明

本项目 **astrbot_plugin_heybox_poster** 以 [MIT License](LICENSE) 发布。

开发过程中参考了下列公开项目。除「测试数据」一节说明的样本外，本项目未复制其源代码。

## xiaoheihe-cli — MIT

`core/sign.py` 的 `hkey` 变换与 `core/client.py` 的 `_rnd`（Workshop v15）公式
参考并改编自该项目。按其 MIT 许可要求，完整保留以下声明：

```text
MIT License

Copyright (c) 2025 Li Fangyu

https://github.com/AaronZ345/xiaoheihe-cli

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## xiaoheihe-api — GPL-3.0（仅测试数据）

`tests/test_core.py` 中的 `SAMPLES`（6 组 `hkey` 输入/输出）取自该项目的公开自检数据：

- 仓库：<https://github.com/56dz/xiaoheihe-api>
- 许可：GPL-3.0
- 范围：仅作为签名算法的回归测试向量，**不包含其源代码**
- 本项目未整体采用 GPL-3.0；如不接受该引用，可删除 `SAMPLES` 常量及
  `test_hkey_matches_captured_samples` 用例，或替换为自己抓取的样本

## 其他参考项目（未使用代码）

| 项目 | 协议 | 说明 |
|---|---|---|
| [ylty1516/xiaoheihe-agent](https://github.com/ylty1516/xiaoheihe-agent) | MIT（`pyproject.toml` 声明；仓库未附 LICENSE 文件） | 长文必填字段与发帖踩坑记录 |
| [wsmxd/XiaoheiheMcpServer](https://github.com/wsmxd/XiaoheiheMcpServer) | MIT（© 2026 wsmxd） | 仅作 Playwright 方案对比，未采用代码 |
| [674537331/astrbot_plugin_xiaoheihe_adapter](https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter) | MIT（© 2026 RyanVaderAn） | 参考其 `pages/` + `register_web_api` + Dashboard 认证流程以及 `qr` / `qr_ticket` 参数形态；页面、状态管理和测试独立实现，未复制代码 |

## 平台说明

小黑盒（Heybox / xiaoheihe）的接口与签名算法来自其公开前端资源。本项目仅供学习与个人使用，
请遵守平台用户协议与社区规则，控制发帖频率，勿用于批量发布或商业用途。
