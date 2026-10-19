// SPDX-License-Identifier: MIT
// frame-care-toast: frame-care のカードを Steam Frame の SteamVR に、視界の正面のトーストとして出す。
//
// 標準入力から 1 行 1 枚（種類 TAB タイトル TAB 本文）を読み、順に出し終わったら SteamVR から切断して終わる。
// SteamVR を起動しない: 自分の vrserver が動いていなければつながない。つなぐときも VRApplication_Background なので、
// その間に SteamVR が終わっていても起動はせず、VRInitError_Init_NoServerForBackgroundApp で終わる（終了コード 3）。
// うまくいったときは何も出力しない。描画は cairo + pango、画像は SetOverlayRaw で渡す。
//
// 終了コード: 0 = 全部出した、1 = エラー、2 = 使い方の間違い、3 = SteamVR が動いていない、4 = 途中で SteamVR が終わった
#include "openvr.h"
#include "card.h"

#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

static const char* kVersion = "0.2.0";
static const char* kOverlayKey = "cympfh.frame-care.toast";
static volatile sig_atomic_t g_stop = 0;

enum Exit { kOk = 0, kError = 1, kUsage = 2, kNoSteamVr = 3, kSteamVrQuit = 4 };

static void errf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
static void errf(const char* fmt, ...) {
    fputs("frame-care-toast: ", stderr);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

struct Options {
    double duration_s = 4.0;   // 1 枚を出す秒数（急ぎは 1.5 倍）
    double width_m = 0.30;
    double distance_m = 0.8;
    double offset_y_m = -0.18;   // 視線より少し下
    std::string font = "Noto Sans CJK JP";
};

// 自分（同じ uid）の vrserver が動いているか。ゾンビ（/proc/PID/stat の状態が Z）は数えない
static bool steamvr_running() {
    DIR* d = opendir("/proc");
    if (!d) return false;
    const uid_t me = getuid();
    bool found = false;
    while (dirent* e = readdir(d)) {
        const char* n = e->d_name;
        bool digits = *n != '\0';
        for (const char* p = n; *p; p++) digits = digits && *p >= '0' && *p <= '9';
        if (!digits) continue;
        std::string dir = std::string("/proc/") + n;
        struct stat st;
        if (::stat(dir.c_str(), &st) != 0 || st.st_uid != me) continue;
        std::ifstream f(dir + "/stat");
        std::string line;
        if (!std::getline(f, line)) continue;
        size_t open = line.find('('), close = line.rfind(')');   // 名前は最初の ( から最後の ) まで
        if (open == std::string::npos || close == std::string::npos || close < open || close + 2 >= line.size()) continue;
        if (line.compare(open + 1, close - open - 1, "vrserver") == 0 && line[close + 2] != 'Z') { found = true; break; }
    }
    closedir(d);
    return found;
}

static double now_s() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

// ------------------------------------------------------------------ SteamVR
struct Vr {
    bool up = false;
    vr::VROverlayHandle_t toast = vr::k_ulOverlayHandleInvalid;
};

static const char* oerr(vr::EVROverlayError e) { return vr::VROverlay()->GetOverlayErrorNameFromEnum(e); }

static void vr_stop(Vr& v) {
    if (!v.up) return;
    vr::VR_Shutdown();
    v.up = false;
    v.toast = vr::k_ulOverlayHandleInvalid;
}

// つなぐ。戻り値は終了コード（kOk ならつながった）
static int vr_start(Vr& v, const Options& o) {
    vr::EVRInitError err = vr::VRInitError_None;
    vr::VR_Init(&err, vr::VRApplication_Background);   // SteamVR が動いていなくても起動しない
    if (err == vr::VRInitError_Init_NoServerForBackgroundApp) return kNoSteamVr;
    if (err != vr::VRInitError_None) {
        errf("SteamVR につながりません: %s", vr::VR_GetVRInitErrorAsEnglishDescription(err));
        return kError;
    }
    vr::EVROverlayError oe = vr::VROverlay()->CreateOverlay(kOverlayKey, "frame-care", &v.toast);
    if (oe != vr::VROverlayError_None) {
        errf("オーバーレイを作れません: %s", oerr(oe));
        vr::VR_Shutdown();
        return kError;
    }
    v.up = true;
    vr::VROverlay()->SetOverlayWidthInMeters(v.toast, float(o.width_m));
    vr::VROverlay()->SetOverlaySortOrder(v.toast, 100);
    return kOk;
}

// 画像をオーバーレイに渡す。カードは見せる前（透明のうち）に 1 回渡すだけなので、SetOverlayRaw で足りる
static bool upload(Vr& v, const std::vector<uint8_t>& rgba) {
    vr::EVROverlayError e =
        vr::VROverlay()->SetOverlayRaw(v.toast, const_cast<uint8_t*>(rgba.data()), uint32_t(card::kW), uint32_t(card::kH), 4);
    if (e != vr::VROverlayError_None) errf("SetOverlayRaw: %s", oerr(e));
    return e == vr::VROverlayError_None;
}

// 視界の正面（頭に付いていく）
static void place(Vr& v, const Options& o) {
    vr::HmdMatrix34_t m{};
    m.m[0][0] = m.m[1][1] = m.m[2][2] = 1;
    m.m[1][3] = float(o.offset_y_m);
    m.m[2][3] = float(-o.distance_m);
    vr::VROverlay()->SetOverlayTransformTrackedDeviceRelative(v.toast, vr::k_unTrackedDeviceIndex_Hmd, &m);
}

// SteamVR が終わる知らせが来たら、受け取ったと返事をして true
static bool quit_requested() {
    vr::VREvent_t ev;
    bool quit = false;
    while (vr::VRSystem()->PollNextEvent(&ev, sizeof ev))
        if (ev.eventType == vr::VREvent_Quit) quit = true;
    if (quit) vr::VRSystem()->AcknowledgeQuit_Exiting();
    return quit;
}

// 1 枚出す（フェードイン・アウト付き）。SteamVR が終わったら false
static bool show(Vr& v, const Options& o, const card::Card& c) {
    cairo_surface_t* s = card::render(c, o.font);
    std::vector<uint8_t> px = card::to_rgba(s);
    cairo_surface_destroy(s);
    if (!upload(v, px)) return true;   // この 1 枚はあきらめて次へ
    place(v, o);
    vr::VROverlay()->SetOverlayAlpha(v.toast, 0.0f);
    vr::VROverlay()->ShowOverlay(v.toast);
    const double dur = o.duration_s * (card::is_alert(c.kind) ? 1.5 : 1.0), fade = 0.4, start = now_s();
    double next_alive_check = start + 1;
    while (!g_stop) {
        double now = now_s(), age = now - start;
        if (quit_requested()) return false;
        if (now >= next_alive_check) {   // 知らせなしに SteamVR が落ちたときも、それ以上呼ばない
            next_alive_check = now + 1;
            if (!steamvr_running()) return false;
        }
        if (age >= dur) break;
        double alpha = age < fade ? age / fade : (dur - age < fade ? (dur - age) / fade : 1.0);
        vr::VROverlay()->SetOverlayAlpha(v.toast, float(alpha));
        std::this_thread::sleep_for(std::chrono::milliseconds(16));
    }
    vr::VROverlay()->HideOverlay(v.toast);
    return true;
}

static int usage() {
    fprintf(stderr,
            "使い方: frame-care-toast [オプション] < カード\n"
            "  標準入力: 1 行 1 枚「種類 TAB タイトル TAB 本文」（種類は alert / warn / care、.temp などでアイコン）\n"
            "  --duration-s 秒 --width-m M --distance-m M --offset-y-m M --font 名前\n"
            "  --dump-png OUT --kind K --title T --text S   カードを PNG に書き出す（SteamVR なしで）\n"
            "  --self-test\n"
            "  --version\n");
    return kUsage;
}

int main(int argc, char** argv) {
    Options o;
    std::string dump_png;
    card::Card dump{"care.break", "休憩しよう", "連続 60 分かぶっています。少し外して目と首を休めませんか"};
    try {
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i];
            auto next = [&]() {
                if (i + 1 >= argc) throw std::invalid_argument(a);
                return std::string(argv[++i]);
            };
            if (a == "--version") { printf("frame-care-toast %s\n", kVersion); return kOk; }
            if (a == "--self-test") {
                int f = card::self_test();
                if (f) printf("自己テスト失敗 %d 件\n", f);
                return f ? kError : kOk;
            }
            if (a == "--duration-s") o.duration_s = std::stod(next());
            else if (a == "--width-m") o.width_m = std::stod(next());
            else if (a == "--distance-m") o.distance_m = std::stod(next());
            else if (a == "--offset-y-m") o.offset_y_m = std::stod(next());
            else if (a == "--font") o.font = next();
            else if (a == "--dump-png") dump_png = next();
            else if (a == "--kind") dump.kind = next();
            else if (a == "--title") dump.title = next();
            else if (a == "--text") dump.text = next();
            else return usage();
        }
    } catch (const std::exception&) {
        return usage();
    }

    if (!dump_png.empty()) {
        cairo_surface_t* s = card::render(dump, o.font);
        cairo_status_t st = cairo_surface_write_to_png(s, dump_png.c_str());
        cairo_surface_destroy(s);
        if (st != CAIRO_STATUS_SUCCESS) { errf("PNG を書けません: %s", dump_png.c_str()); return kError; }
        return kOk;
    }

    std::vector<card::Card> cards;
    std::string line;
    while (std::getline(std::cin, line)) {
        card::Card c;
        if (card::parse_line(line, c)) cards.push_back(c);
        else if (!line.empty()) errf("読めない行を捨てました");
    }
    if (cards.empty()) return kOk;
    if (!steamvr_running()) return kNoSteamVr;

    signal(SIGTERM, [](int) { g_stop = 1; });
    signal(SIGINT, [](int) { g_stop = 1; });
    Vr v;
    if (int rc = vr_start(v, o)) return rc;
    int rc = kOk;
    for (size_t i = 0; i < cards.size() && !g_stop; i++) {
        if (!show(v, o, cards[i])) {
            rc = kSteamVrQuit;
            break;
        }
        if (i + 1 < cards.size()) std::this_thread::sleep_for(std::chrono::milliseconds(300));
    }
    vr_stop(v);
    return rc;
}
