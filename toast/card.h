// SPDX-License-Identifier: MIT
// frame-care のカード（640x160）を cairo + pango で描く。SteamVR とは関係ない部分だけ（テストしやすくするため）。
// 見た目は vrc-wrist-notifier の Frame 版（wrist-toast 0.4.5）のカードと同じ。
#pragma once

#include <cairo.h>
#include <pango/pangocairo.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace card {

static const int kW = 640, kH = 160;

struct Card {
    std::string kind, title, text;   // kind は「基本.アイコン」（例: alert.temp、care.break）
};

struct Rgb { double r, g, b; };

// 種類: alert / urgent = 赤（急ぎ。表示も長め）、warn = 琥珀、care / info = 青緑。「.」のあとはアイコン
inline std::string kind_base(const std::string& kind) { return kind.substr(0, kind.find('.')); }
inline std::string kind_sub(const std::string& kind) {
    size_t d = kind.find('.');
    return d == std::string::npos ? "" : kind.substr(d + 1);
}
inline bool is_alert(const std::string& kind) {
    std::string b = kind_base(kind);
    return b == "alert" || b == "urgent";
}

inline const char* icon(const std::string& kind) {
    std::string sub = kind_sub(kind), base = kind_base(kind);
    if (sub == "temp") return "🌡\uFE0F";   // U+1F321 は文字のままだと白黒になるので、絵文字にする印（VS16）を付ける
    if (sub == "battery") return "🔋";
    if (sub == "charge") return "⚡";
    if (sub == "break") return "☕";
    if (sub == "water") return "💧";
    if (is_alert(kind)) return "!";
    if (base == "warn") return "⚠";
    return "♥";
}

inline Rgb color(const std::string& kind) {
    std::string base = kind_base(kind);
    if (is_alert(kind)) return {0.93, 0.27, 0.27};   // 赤
    if (base == "warn") return {1.00, 0.58, 0.10};   // 琥珀
    return {0.18, 0.76, 0.70};                       // 青緑（care / info / そのほか）
}

// 双方向テキストの制御文字（LRM/RLM、U+202A〜202E、U+2066〜2069、ALM）。表示を入れ替えられるので落とす
inline size_t bidi_control_len(const std::string& s, size_t i) {
    auto b = [&](size_t k) { return i + k < s.size() ? static_cast<unsigned char>(s[i + k]) : 0; };
    if (b(0) == 0xE2 && b(1) == 0x80 && (b(2) == 0x8E || b(2) == 0x8F || (b(2) >= 0xAA && b(2) <= 0xAE))) return 3;
    if (b(0) == 0xE2 && b(1) == 0x81 && b(2) >= 0xA6 && b(2) <= 0xA9) return 3;
    if (b(0) == 0xD8 && b(1) == 0x9C) return 2;   // U+061C
    return 0;
}

// 制御文字（C0、DEL、双方向の制御文字）を落として、長すぎる文字列を切る（UTF-8 の途中で切らない）
inline std::string clean(const std::string& s, size_t max_bytes) {
    std::string out;
    for (size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (size_t n = bidi_control_len(s, i)) { i += n; continue; }
        i++;
        if (c < 0x20 || c == 0x7F) continue;
        out.push_back(char(c));
    }
    if (out.size() > max_bytes) {
        size_t n = max_bytes;
        while (n > 0 && (static_cast<unsigned char>(out[n]) & 0xC0) == 0x80) n--;
        out = out.substr(0, n) + "…";
    }
    return out;
}

// 1 行「種類 TAB タイトル TAB 本文」を読む（本文が無くてもよい）
inline bool parse_line(const std::string& line, Card& c) {
    size_t a = line.find('\t');
    if (a == std::string::npos) return false;
    size_t b = line.find('\t', a + 1);
    c.kind = clean(line.substr(0, a), 24);
    c.title = clean(line.substr(a + 1, b == std::string::npos ? std::string::npos : b - a - 1), 120);
    c.text = b == std::string::npos ? "" : clean(line.substr(b + 1), 240);
    return !c.kind.empty() && !c.title.empty();
}

inline void rounded(cairo_t* cr, double x, double y, double w, double h, double r) {
    r = std::min(r, std::min(w, h) / 2);
    cairo_new_sub_path(cr);
    cairo_arc(cr, x + w - r, y + r, r, -M_PI / 2, 0);
    cairo_arc(cr, x + w - r, y + h - r, r, 0, M_PI / 2);
    cairo_arc(cr, x + r, y + h - r, r, M_PI / 2, M_PI);
    cairo_arc(cr, x + r, y + r, r, M_PI, 3 * M_PI / 2);
    cairo_close_path(cr);
}

// 文字を描く。y は文字の縦の中心。width > 0 なら、はみ出す分を「…」にする
inline void text(cairo_t* cr, const std::string& font, const std::string& str, const char* weight, int size, double x,
                 double y, int width, Rgb c, bool center = false) {
    PangoLayout* layout = pango_cairo_create_layout(cr);
    std::string desc = font + " " + weight + " " + std::to_string(size);
    PangoFontDescription* fd = pango_font_description_from_string(desc.c_str());
    pango_layout_set_font_description(layout, fd);
    if (width > 0) {
        pango_layout_set_width(layout, width * PANGO_SCALE);
        pango_layout_set_ellipsize(layout, PANGO_ELLIPSIZE_END);
    }
    pango_layout_set_single_paragraph_mode(layout, TRUE);
    pango_layout_set_text(layout, str.c_str(), -1);
    int lw, lh;
    pango_layout_get_pixel_size(layout, &lw, &lh);
    cairo_set_source_rgb(cr, c.r, c.g, c.b);
    cairo_move_to(cr, center ? x - lw / 2.0 : x, y - lh / 2.0);
    pango_cairo_show_layout(cr, layout);
    pango_font_description_free(fd);
    g_object_unref(layout);
}

// カードを描く（呼んだ側が cairo_surface_destroy する）
inline cairo_surface_t* render(const Card& t, const std::string& font) {
    cairo_surface_t* s = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, kW, kH);
    cairo_t* cr = cairo_create(s);
    bool alert = is_alert(t.kind);
    rounded(cr, 4, 4, kW - 8, kH - 8, 28);
    if (alert) cairo_set_source_rgba(cr, 0.22, 0.06, 0.07, 0.94);   // 急ぎは背景も赤み
    else cairo_set_source_rgba(cr, 0.08, 0.09, 0.11, 0.90);
    cairo_fill_preserve(cr);
    Rgb c = color(t.kind);
    cairo_set_source_rgba(cr, c.r, c.g, c.b, 0.9);
    cairo_set_line_width(cr, alert ? 8 : 4);
    cairo_stroke(cr);
    cairo_arc(cr, 76, kH / 2.0, 46, 0, 2 * M_PI);
    cairo_set_source_rgba(cr, c.r, c.g, c.b, 0.95);
    cairo_fill(cr);
    text(cr, font, icon(t.kind), "Bold", 34, 76, kH / 2.0, 0, {0.08, 0.09, 0.11}, true);
    text(cr, font, t.title, "Bold", 26, 142, 50, kW - 170, {1, 1, 1});
    text(cr, font, t.text, "Regular", 22, 142, 104, kW - 170, {0.82, 0.84, 0.88});
    cairo_destroy(cr);
    return s;
}

// cairo の ARGB32（乗算済み）を、OpenVR に渡す非乗算の RGBA にする
inline std::vector<uint8_t> to_rgba(cairo_surface_t* s) {
    cairo_surface_flush(s);
    int w = cairo_image_surface_get_width(s), h = cairo_image_surface_get_height(s);
    int stride = cairo_image_surface_get_stride(s);
    const uint8_t* src = cairo_image_surface_get_data(s);
    std::vector<uint8_t> rgba(size_t(w) * h * 4);
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            uint32_t px;
            memcpy(&px, src + y * stride + x * 4, 4);
            uint8_t a = px >> 24, r = (px >> 16) & 0xff, g = (px >> 8) & 0xff, b = px & 0xff;
            uint8_t* d = &rgba[(size_t(y) * w + x) * 4];
            if (a) {
                d[0] = uint8_t(std::min(255, r * 255 / a));
                d[1] = uint8_t(std::min(255, g * 255 / a));
                d[2] = uint8_t(std::min(255, b * 255 / a));
            } else {
                d[0] = d[1] = d[2] = 0;
            }
            d[3] = a;
        }
    }
    return rgba;
}

// 自己テスト（失敗した数を返す）
inline int self_test() {
    int fails = 0;
    auto check = [&](bool ok, const char* what) {
        if (!ok) { printf("FAIL %s\n", what); fails++; }
    };
    Rgb c = color("alert.temp");
    check(c.r > 0.9 && c.g < 0.4 && std::string(icon("alert.temp")) == "🌡\uFE0F" && is_alert("urgent"), "alert.temp は赤で温度計");
    c = color("warn.battery");
    check(c.r > 0.9 && c.g > 0.5 && c.b < 0.2 && std::string(icon("warn.battery")) == "🔋", "warn.battery は琥珀で電池");
    c = color("care.break");
    check(c.g > 0.7 && c.b > 0.6 && c.r < 0.3 && std::string(icon("care.break")) == "☕" && !is_alert("care.break"),
          "care.break は青緑でカップ");
    check(std::string(icon("warn")) == "⚠" && std::string(icon("alert")) == "!" && std::string(icon("care")) == "♥",
          "アイコンの無い種類");
    check(clean("a\nb\tc\x7f", 10) == "abc", "制御文字と DEL を落とす");
    check(clean("x\u202Eevil\u202C\u2066y\u2069\u200F\u061C", 64) == "xevily", "双方向の制御文字を落とす");
    check(clean("aあい", 2) == "a…" && clean("aあい", 4) == "aあ…", "文字の途中で切るときは、その文字の前で切る");
    std::string longjp;
    for (int i = 0; i < 100; i++) longjp += "あ";
    std::string cut = clean(longjp, 31);
    check(cut.size() <= 31 + 3 && cut.substr(cut.size() - 3) == "…", "UTF-8 の途中で切らない");
    Card k;
    check(parse_line("care.break\t休憩しよう\t本文", k) && k.kind == "care.break" && k.title == "休憩しよう" && k.text == "本文",
          "1 行を読む");
    check(parse_line("warn\tタイトルだけ", k) && k.text.empty(), "本文が無くても読む");
    check(!parse_line("ゴミ", k) && !parse_line("\tタイトル\t本文", k), "読めない行は捨てる");
    check(!parse_line("care\t\t本文", k) && !parse_line("care\t\x01\u202E\t本文", k), "タイトルが空（制御文字だけも）なら捨てる");
    {
        // 乗算済み ARGB32 → 非乗算 RGBA: 半透明の赤 (a=128, r=128) は r=255 に戻る。完全に透明なら色は 0
        cairo_surface_t* one = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, 2, 1);
        cairo_surface_flush(one);
        uint32_t px2[2] = {(128u << 24) | (128u << 16) | (64u << 8) | 0u, 0u};
        memcpy(cairo_image_surface_get_data(one), px2, sizeof px2);
        cairo_surface_mark_dirty(one);
        std::vector<uint8_t> o = to_rgba(one);
        cairo_surface_destroy(one);
        check(o[0] == 255 && o[1] == 127 && o[2] == 0 && o[3] == 128 && o[4] == 0 && o[7] == 0, "乗算済みの色を元に戻す");
    }
    cairo_surface_t* s = render({"alert.temp", "本体が高温 91°C", "いったん休ませてください"}, "Noto Sans CJK JP");
    std::vector<uint8_t> px = to_rgba(s);
    cairo_surface_destroy(s);
    size_t mid = (size_t(kH / 2) * kW + 300) * 4, corner = 0;
    check(px.size() == size_t(kW) * kH * 4 && px[mid + 3] > 200 && px[corner + 3] == 0, "カードを描ける（角は透明）");
    return fails;
}

}  // namespace card
