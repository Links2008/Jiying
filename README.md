<div align="center">
  <img src="assets/jiying-logo.png" width="88" alt="即影图标">
  <h1>即影 Jiying</h1>
  <p><strong>从网里，汲一勺光</strong></p>
  <p>把视频保存到本地，让字幕跨越语言。</p>
  <p>适用于 Windows 的视频下载、字幕识别与翻译工具。</p>
  <p>
    <a href="https://apps.microsoft.com/detail/9N2XR4X7CXPR"><strong>从 Microsoft Store 下载</strong></a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Links2008/Jiying/releases">GitHub 版本与下载</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Links2008/Jiying/issues">问题反馈</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078d4?style=flat-square" alt="Windows 10 和 11">
    <img src="https://img.shields.io/badge/架构-x64-5b6473?style=flat-square" alt="x64">
    <a href="LICENSE"><img src="https://img.shields.io/badge/许可-MIT-22a06b?style=flat-square" alt="MIT License"></a>
  </p>
</div>

<p align="center">
  <img src="assets/store-overview.jpg" width="100%" alt="即影界面概览">
</p>

## 下载与安装

推荐通过 **[Microsoft Store 安装即影](https://apps.microsoft.com/detail/9N2XR4X7CXPR)**，获取商店签名的安装版本和后续更新。

系统要求：Windows 10 2004 或更新版本 / Windows 11，x64。

[GitHub Releases](https://github.com/Links2008/Jiying/releases) 提供独立的版本记录和历史下载。两个渠道的版本可能不同，请以各自页面为准。便携版请下载 ZIP，完整解压后运行 `Jiying.Native.exe`；MSIX 的安装条件请查看对应版本说明。

## 三步开始

1. **粘贴链接**：打开即影，粘贴具体视频的链接并解析。
2. **选择下载内容**：选择可用画质、格式和保存位置。
3. **按需生成字幕**：选择原语言字幕或翻译字幕，再开始任务；只需要视频时，可关闭字幕处理。

可选画质取决于原视频和网站提供的资源。需要登录的视频，也需要你拥有对应的访问权限。

## 能做什么

| 功能 | 使用方式 |
| --- | --- |
| 视频下载 | 解析链接，选择网站实际提供的分辨率和格式 |
| 任务管理 | 查看进度，取消、重试，并恢复未完成任务 |
| 本地字幕识别 | 使用 Whisper 语音模型，在设备上处理音频 |
| 云端识别 | 在设置中配置所选服务及其接口 |
| 多语言翻译 | 配置翻译服务，将识别后的字幕转换为目标语言 |
| 字幕输出 | 保存字幕文件，或将字幕烧录到视频中 |

本地识别需要先下载语音模型；首次使用时按提示准备即可。只下载视频时不需要语音模型。云端识别和翻译需要你配置对应服务，服务的可用性和费用由其提供商决定。

## 界面预览

<p align="center">
  <img src="assets/store-link-analysis.jpg" width="49%" alt="粘贴视频链接并解析">
  <img src="assets/store-quality-options.jpg" width="49%" alt="选择画质、格式和下载选项">
</p>
<p align="center">
  <img src="assets/store-subtitles-settings.jpg" width="100%" alt="字幕识别与翻译设置">
</p>

截图为界面示例，具体布局以所安装版本为准。

## 常见问题

**解析失败或没有期望的画质？**  
确认链接指向具体视频且仍可访问，再检查网站是否要求登录。原视频的清晰度、账号权限和网站限制都会影响可下载资源。反馈时请附软件版本、平台、错误提示和可公开的示例链接。

**GitHub 的 MSIX 无法安装？**  
历史发布中的 MSIX 可能未使用受信任证书签名，请先查看该版本说明。推荐直接从 [Microsoft Store](https://apps.microsoft.com/detail/9N2XR4X7CXPR) 安装。

**字幕识别不准确？**  
音频质量、语言选择和语音模型会影响结果。可检查识别语言、尝试更合适的模型，或修改导出的字幕文件。自动识别与翻译结果仍需人工校对。

## 隐私与支持

设置、任务记录、下载文件和本地识别数据保存在你的设备上。解析和下载会访问对应视频网站；主动使用云端识别或翻译时，相关音频或字幕会发送给你配置的服务商。

- [隐私政策](https://links2008.github.io/Jiying/privacy.html)
- [提交问题或建议](https://github.com/Links2008/Jiying/issues)
- [MIT 许可证](LICENSE)

请只下载和处理你有权访问的内容。反馈问题时请勿公开 Cookie、API 密钥或其他登录信息。

本仓库用于发布即影安装包、版本说明、隐私政策和问题反馈。

