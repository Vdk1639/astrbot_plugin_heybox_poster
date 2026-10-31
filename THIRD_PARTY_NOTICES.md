# 第三方声明

本项目 **astrbot_plugin_heybox_poster** 以 [MIT License](LICENSE) 发布。

本项目包含下列项目的改编代码、面板资源及测试数据。各部分的来源和保留声明如下。

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

## astrbot_plugin_xiaoheihe_adapter — MIT

- 上游项目：<https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter>
- 原作者及版权人：RyanVaderAn，Copyright (c) 2026 RyanVaderAn。
- 本次整合来源：工作区提供的上游仓库副本，其 `metadata.yaml` 声明版本为 v1.3.2；该副本不含可核实的 Git 提交信息，不标注未经确认的提交哈希。
- 完整原始许可：[third_party/xiaoheihe-adapter.LICENSE](third_party/xiaoheihe-adapter.LICENSE)，从该副本的根目录 `LICENSE` 原样保留。

`dev` 整合直接复制并改编了上游 `xiaoheihe/` 运行时与原生平台模块（包括认证、签名、数据库、通知、主动浏览、事件与模型上下文等），将上游插件入口相关逻辑整理到 `community.py`，并将上游测试复制、适配为 `adapter_tests/`。这些部分是代码整合，不只是实现思路参考。

管理面板从上游 `pages/xiaoheihe/` 复制至 `pages/account/`，保留其 HTML、样式、浏览来源逻辑和图标；本项目修改插件身份及 API 归属，并加入统一发帖/评论编辑审核、认证 bridge、响应竞争保护和离线交互测试。

整合后的平台类型改为 `heybox_community`，显示为“小黑盒（发帖与评论）”，通过本项目发布服务与审核队列连接原发帖能力；原生平台和面板的上游版权归属不变。

此前 `core/login.py` 已参考其扫码状态、JSON 凭据解析与轮询方案，客户端标识生成公式改编自其 `xiaoheihe/request_signing.py`。

`debug_qr.py` 的对照模式采用其 GET 公共参数与请求头；网络传输、诊断记录和凭据检查复用本项目现有客户端。

以下保留 MIT 许可正文；上游原文件另附于上述许可路径。发布或分发整合代码时应同时保留这些声明。

```text
MIT License

Copyright (c) 2026 RyanVaderAn

https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter

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

## 其他参考项目（未使用代码）

| 项目 | 协议 | 说明 |
|---|---|---|
| [ylty1516/xiaoheihe-agent](https://github.com/ylty1516/xiaoheihe-agent) | MIT（`pyproject.toml` 声明；仓库未附 LICENSE 文件） | 长文必填字段与发帖踩坑记录 |
| [wsmxd/XiaoheiheMcpServer](https://github.com/wsmxd/XiaoheiheMcpServer) | MIT（© 2026 wsmxd） | 仅作 Playwright 方案对比，未采用代码 |

## 平台说明

小黑盒（Heybox / xiaoheihe）的接口与签名算法来自其公开前端资源。本项目仅供学习与个人使用，
请遵守平台用户协议与社区规则，控制发帖频率，勿用于批量发布或商业用途。
