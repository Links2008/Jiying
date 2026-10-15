// 不启动窗口或引擎，验证逐条编辑不会改动序号、时间轴或其他字幕。
using Jiying.Desktop;

var original = "7\r\n00:00:00,000 --> 00:00:01,000\r\n原文\r\n第二行\r\n\r\n9\r\n00:00:01,000 --> 00:00:02,000\r\n下一句\r\n";
var document = SubtitleDocument.Parse(original);
Require(document.Serialize() == original, "打开后必须逐字保留原文件");
document.Cues[0].Text = "修正后\n第二行";
Require(document.Serialize(true) == original.Replace("原文", "修正后"), "只替换所编辑的文字");
document.Cues[0].Text = "";
Throws(() => document.Serialize(true), "空字幕必须拒绝保存");
document.Cues[0].Text = "一行\n\n另一行";
Throws(() => document.Serialize(true), "空白分块不能意外生成新字幕");
Throws(() => SubtitleDocument.Parse("1\n00:00:00,000 --> 00:00:01,000\n有效\n\n损坏块"), "损坏块不能静默丢弃");
Throws(() => SubtitleDocument.Parse("1\n00:00:01,000 --> 00:00:00,000\n倒序"), "无效时间段必须拒绝");
var target = SubtitleDocument.Parse("1\n00:00:00,000 --> 00:00:01,000\n译文一\n\n2\n00:00:01,000 --> 00:00:02,000\n译文二\n");
target.SetReference("1\n00:00:00,000 --> 00:00:02,000\n完整原句\n");
Require(target.Cues.All(c => c.ReferenceText == "完整原句"), "原文必须按时间对齐，不能按条目数量对齐");
target.SetReference("1\n00:00:10,000 --> 00:00:11,000\n无关原句\n");
Require(target.Cues.All(c => c.ReferenceText == "未找到对应原文"), "不能借用其他时间的原文");
Console.WriteLine("通过：字幕原稿、时间轴、多行、格式拒绝和原译文时间对齐。");

static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }
static void Throws(Action action, string message)
{
    try { action(); }
    catch (FormatException) { return; }
    throw new Exception(message);
}
