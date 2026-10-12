using System.Windows.Controls;
using System.Windows.Markup;
using System.Windows.Media;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private void BuildFontChoices()
    {
        var english = XmlLanguage.GetLanguage("en-us");
        var chinese = XmlLanguage.GetLanguage("zh-cn");
        var seen = FontNameBox.Items.OfType<ComboBoxItem>().Select(item => item.Tag?.ToString())
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        foreach (var family in Fonts.SystemFontFamilies.OrderBy(family => family.Source, StringComparer.CurrentCultureIgnoreCase))
        {
            // Composite fallback families are not physical fonts that libass can select.
            if (!family.GetTypefaces().Any(face => face.TryGetGlyphTypeface(out _))) continue;
            var name = family.FamilyNames.TryGetValue(english, out var canonical) ? canonical : family.Source;
            if (!seen.Add(name)) continue;
            var label = family.FamilyNames.TryGetValue(chinese, out var local) ? local : name;
            FontNameBox.Items.Add(new ComboBoxItem { Tag = name, Content = label == name ? name : $"{label} · {name}" });
        }
        if (!string.IsNullOrWhiteSpace(_ui.FontName) && seen.Add(_ui.FontName))
            FontNameBox.Items.Add(new ComboBoxItem { Tag = _ui.FontName, Content = $"{_ui.FontName}（未安装）", ToolTip = "安装此字体后重启即影；当前预览可能使用系统回退字体。" });
    }

    private void FontSearch_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_loadingControls || FontNameBox is null) return;
        var query = FontSearchBox.Text.Trim();
        var selected = FontNameBox.SelectedItem;
        FontNameBox.Items.Filter = item => ReferenceEquals(item, selected) || item is ComboBoxItem font &&
            (font.Tag?.ToString()?.Contains(query, StringComparison.OrdinalIgnoreCase) == true ||
             font.Content?.ToString()?.Contains(query, StringComparison.CurrentCultureIgnoreCase) == true);
        FontSearchPlaceholder.Visibility = query.Length == 0 ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;
    }
}
