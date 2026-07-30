# -*- coding: utf-8 -*-
"""DACNV 機材リストに「輸送分類」「再購入リスト」「重要品目メモ」シートを追加する。"""
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

HERE = Path(__file__).parent
SRC = HERE / "DACNV_Equipment_List_1.xlsx"
DST = HERE / "DACNV_Equipment_List_輸送分類.xlsx"


def setup_print(ws, header_row=None, landscape=True, fit_width=True, paper="A4"):
    """印刷用の共通設定：用紙・改ページ位置での見出し繰り返し・余白・拡大縮小。

    列数の多い表は A4 に収めると実効フォントが 5pt 以下になり印刷しても読めないため、
    paper="A3" を指定して用紙側で幅を稼ぐ。
    """
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3 if paper == "A3" else ws.PAPERSIZE_A4
    ws.page_setup.fitToPage = fit_width
    if fit_width:
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = fit_width
    ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5,
                                   header=0.2, footer=0.2)
    ws.print_options.horizontalCentered = False
    if header_row:
        ws.print_title_rows = f"{header_row}:{header_row}"
    ws.oddFooter.center.text = "&P / &N ページ"
    ws.oddFooter.right.text = "&D"

C1 = "①発送のみ"
C2 = "②機内持込のみ"
C3 = "③両方可"
C4 = "④両方不可"

OK = "可"
COND = "条件付き可"
NG = "不可"

NONE_DG = "該当なし"

# row -> (分類, 機内持込, 預入, 発送, 危険物, 輸出令想定, 要確認, 理由, 推奨)
T = {
 5: (C2, OK, COND, NG, NONE_DG, "非該当（推定・工業用ダイヤ）", "○",
     "太田研からの借用品。NV注入済みで代替品が存在せず、紛失・破損時に弁償不能。規則上は発送可だが実務上は発送不可と扱う。",
     "手荷物で持参（小型ケースに入れ常時携行）"),
 6: (C2, OK, COND, NG, NONE_DG, "非該当（推定・工業用ダイヤ）", "",
     "太田研・荒井研からの借用品。上記と同じ理由で発送に載せない。",
     "手荷物で持参"),
 7: (C2, OK, COND, NG, NONE_DG, "第2項（先端材料）該当の可能性", "○",
     "荒井研・太田研からの借用品で代替不可のため発送しない。加えてベリリウム銅は含有率次第で別表第1第2項の規制対象になり得る。",
     "手荷物で持参＋材質証明（Be含有率）を入手して該非判定に添付"),
 8: (C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "セラミック製の小物。輸送上の制約なし。", "ダイヤと一緒に持参"),
 9: (C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "金属薄板の小物。輸送上の制約なし。", "持参（予備を多めに）"),
 10:(C4, NG, NG, COND, "引火性液体／腐食性の可能性（主剤・硬化剤）", "非該当（推定）", "○",
     "エポキシ接着剤は硬化剤側がUN分類に該当し得る。旅客機の手荷物・預入とも実質不可、クーリエも危険物申告が必要で費用対効果が悪い。",
     "現地調達（ドイツで同等品を購入）"),
 11:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "貴金属だが少量の箔。輸送上の制約なし。", "持参（少量・高価のため）"),
 12:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 13:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "同軸ケーブル。制約なし。", "発送"),
 14:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "同軸ケーブル。制約なし。", "発送"),
 15:(C3, COND, OK, OK, NONE_DG, "第9項（貨物等省令第8条）該当可能性あり", "○",
     "電池・ガスを含まず危険物非該当のため規則上は持込可。金属塊でX線に強く写るため開披検査を受ける前提。動作帯域・出力から第9項該当を否定しきれず、Mini-Circuits の該非判定書／ECCN の入手が必須。",
     "機内持込を推奨（英文仕様書を携行）"),
 16:(C3, COND, OK, OK, NONE_DG, "第9項該当の可能性（最優先で要確認）", "○",
     "広帯域シンセサイザ。本リスト中で最も規制該当リスクが高い。輸送自体は制約なし。",
     "機内持込。該非判定書を最優先で取得し、該当なら輸出許可申請が必要"),
 17:(C1, NG, COND, OK, NONE_DG, "非該当（推定）", "",
     "重量・容積が大きく手荷物に収まらない。100V機のためドイツでは変圧器または現地電源の手当てが要る。",
     "発送（または現地で230V対応電源を調達する方が安い可能性あり）"),
 18:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "受動部品。制約なし。", "発送"),
 19:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "受動部品。制約なし。", "発送"),
 20:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "",
     "先端が鋭利な工具のためキャビン持込不可。預入または発送なら可。100V機の電圧に注意。",
     "発送（または現地調達。230V対応品の方が確実）"),
 21:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 22:(C3, COND, COND, OK, NONE_DG, "第6項（貨物等省令第6条）／非該当の公算", "○",
     "DPSSレーザーで電池・ガスを含まずIATA上は危険物非該当、規則上は持込可能。障壁は保安検査官の裁量（レーザーポインタとの混同）と経由国・ドイツの高出力レーザー規制。CW 532nm/300mW は第6項の出力しきい値を下回り非該当の公算が大きいが、CNI発行の該非判定書が必須。（元表の備考は発注時点のもので、現物は入手済み。購入時の純正梱包材が保管されているか確認すること）",
     "機内持込を強く推奨（下記「重要品目メモ」の準備物を揃えること）"),
 23:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "高価・小型・衝撃に弱い。", "持参"),
 24:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "5枚。制約なし。", "発送"),
 25:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "光学薄膜品。破損に注意。", "持参"),
 26:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "光学薄膜品。破損に注意。", "持参"),
 27:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "光学薄膜品。破損に注意。", "持参"),
 28:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "持参"),
 29:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "焦点距離は現地の光学配置に依存するため、現地調達も選択肢。", "発送（または現地調達）"),
 30:(C1, NG, COND, OK, NONE_DG, "非該当（推定）", "",
     "精密ステージ。重量・容積で手荷物に収まらない。", "発送（緩衝材を厚めに）"),
 31:(C1, NG, COND, OK, NONE_DG, "非該当（推定）", "", "金属治具。重量のため発送。", "発送"),
 32:(C1, NG, COND, OK, NONE_DG, "非該当（推定）", "", "オーダーメイド品で再製作に時間がかかるため確実に発送する。", "発送（図面も現地に持参）"),
 33:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 34:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 35:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 36:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "ACアダプタ。ドイツの230V/Cタイプ対応を確認すること。", "発送（プラグ変換要）"),
 37:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "持参"),
 38:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "金属工具類。キャビン持込は保安検査で止められる可能性が高い。", "発送"),
 39:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "12個。重量のため発送。", "発送"),
 40:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "12個。重量のため発送。", "発送"),
 41:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "重量のため発送。", "発送"),
 42:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "重量のため発送。", "発送"),
 43:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "金属棒状。キャビン持込は不可と考える。", "発送"),
 44:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "金属棒状。キャビン持込は不可と考える。", "発送"),
 45:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "4個。重量のため発送。", "発送"),
 46:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "小型。制約なし。", "発送"),
 47:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 48:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "制約なし。", "発送"),
 49:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "細長い金属ロッド10本。キャビン持込は保安検査で止められる。", "発送"),
 50:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "型番未確定のため発注前に確定させること。", "発送"),
 51:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "cMOSとレンズマウントの接続用。型番未確定。", "発送"),
 52:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "LEDコリメータとケージの接続用。型番未確定。", "発送"),
 53:(C3, OK, OK, OK, NONE_DG, "非該当（推定）", "", "レーザーを持参するなら保護メガネも同時に携行すること。", "持参（レーザーと同梱しない）"),
 54:(C1, NG, OK, OK, NONE_DG, "非該当（推定）", "", "工具はキャビン持込不可。預入または発送。", "発送（現地調達も容易）"),
 55:(C1, NG, NG, OK, NONE_DG, "非該当（推定）", "", "長尺・重量物。手荷物・預入とも寸法制限を超える。", "発送（または現地調達）"),
 56:(C1, NG, COND, OK, NONE_DG, "非該当（推定）", "", "板状の大物。寸法制限に注意。", "発送（または現地調達）"),
 57:(C3, OK, COND, OK, NONE_DG, "非該当（推定）", "", "精密機器。振動・衝撃に弱いため預入は避ける。", "持参"),
}

wb = openpyxl.load_workbook(SRC)
src = wb["機材一覧"]

items = []
system = None
for r in range(5, 58):
    sysv = src.cell(r, 1).value
    if sysv:
        system = sysv
    name = src.cell(r, 2).value
    if not name:
        continue
    items.append({
        "row": r, "system": system, "name": name,
        "maker": src.cell(r, 3).value, "model": src.cell(r, 4).value,
        "qty": src.cell(r, 5).value, "note": src.cell(r, 10).value,
    })

missing = [i["row"] for i in items if i["row"] not in T]
assert not missing, f"分類データ欠落: {missing}"

# ---------- 共通スタイル ----------
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CTR = Alignment(horizontal="center", vertical="center", wrap_text=True)

CAT_FILL = {
    C1: PatternFill("solid", fgColor="DDEBF7"),
    C2: PatternFill("solid", fgColor="FFF2CC"),
    C3: PatternFill("solid", fgColor="E2EFDA"),
    C4: PatternFill("solid", fgColor="FCE4E4"),
}
JUDGE_FILL = {
    OK:   PatternFill("solid", fgColor="E2EFDA"),
    COND: PatternFill("solid", fgColor="FFF2CC"),
    NG:   PatternFill("solid", fgColor="FCE4E4"),
}


def style_header(ws, row, ncol):
    for c in range(1, ncol + 1):
        cell = ws.cell(row, c)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = CTR
        cell.border = BORDER


# ================= シート1: 輸送分類 =================
ws = wb.create_sheet("輸送分類", 1)
ws.sheet_view.showGridLines = False

ws["A1"] = "DAC-NV 機材 航空輸送分類表（乗継便・第三国経由を前提）"
ws["A1"].font = Font(bold=True, size=14)
ws["A2"] = ("【前提】①発送のみ ②機内持込のみ ③両方可 ④両方不可。「機内持込」はキャビン持込を指し、"
            "預入手荷物は別列。 【重要】輸出令の項番は公開仕様からの推定であり最終判断ではない。"
            "「要確認」＝○ の品目はメーカー発行の該非判定書（パラメータシート）を取得してから申請書を作成すること。"
            "「条件付き可」は航空会社・経由国の保安検査官の裁量で結果が変わる項目。")
ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
ws.merge_cells("A2:L2")
ws.row_dimensions[2].height = 58

HDRS = ["システム", "アイテム名", "メーカー", "型番", "個数", "分類",
        "機内持込", "預入手荷物", "発送(クーリエ)", "危険物区分",
        "輸出令 想定項番", "要確認", "判断理由・注意点", "推奨"]
HR = 4
for c, h in enumerate(HDRS, 1):
    ws.cell(HR, c, h)
style_header(ws, HR, len(HDRS))

r = HR + 1
for it in items:
    cat, cabin, checked, ship, dg, exp, need, reason, rec = T[it["row"]]
    vals = [it["system"], it["name"], it["maker"], it["model"], it["qty"],
            cat, cabin, checked, ship, dg, exp, need, reason, rec]
    for c, v in enumerate(vals, 1):
        cell = ws.cell(r, c, v)
        cell.border = BORDER
        cell.alignment = WRAP if c in (13, 14) else (CTR if c in (5, 6, 7, 8, 9, 12) else WRAP)
    ws.cell(r, 6).fill = CAT_FILL[cat]
    ws.cell(r, 6).font = Font(bold=True, size=10)
    for c, v in ((7, cabin), (8, checked), (9, ship)):
        ws.cell(r, c).fill = JUDGE_FILL[v]
    if need:
        ws.cell(r, 12).fill = PatternFill("solid", fgColor="FFC7CE")
        ws.cell(r, 12).font = Font(bold=True, color="9C0006")
    r += 1

LAST = r - 1
widths = [13, 26, 12, 18, 5, 11, 9, 9, 11, 18, 20, 6, 36, 22]
for c, w in enumerate(widths, 1):
    ws.column_dimensions[get_column_letter(c)].width = w
ws.auto_filter.ref = f"A{HR}:N{LAST}"
ws.freeze_panes = f"C{HR+1}"

# 凡例
lr = LAST + 2
ws.cell(lr, 1, "凡例").font = Font(bold=True)
legend = [
    (C1, "キャビン持込は不可（寸法・重量・工具規制）。発送または預入で運ぶ。"),
    (C2, "他研究室からの借用品で代替不能なため、実務上は発送に載せず必ず手荷物で運ぶ。"),
    (C3, "規則上どちらでも運べる。「推奨」列で実務上の振り分けを示した。"),
    (C4, "旅客機で運べず、発送も危険物申告が必要。現地調達が現実的。"),
]
for i, (k, d) in enumerate(legend):
    ws.cell(lr + 1 + i, 1, k).fill = CAT_FILL[k]
    ws.cell(lr + 1 + i, 1).font = Font(bold=True)
    ws.cell(lr + 1 + i, 2, d)

# ================= シート2: 再購入リスト =================
ws2 = wb.create_sheet("再購入リスト", 2)
ws2.sheet_view.showGridLines = False
ws2["A1"] = "ドイツ発送分の国内買い直しリスト（発注用）"
ws2["A1"].font = Font(bold=True, size=14)
ws2["A2"] = ("「輸送分類」シートで推奨＝発送 とした品目、つまり実際にドイツへ送る分をセクションAに抽出した。"
             "単価は現時点の見積が無いため空欄とし「要見積」とした。"
             "推測値を入れると発注根拠にならないため、各社の最新見積で埋めること。"
             "セクションBの借用品は購入品ではないため買い直しの要否は借用元との調整事項、"
             "セクションDの持参品は日本から送らないので買い直し対象外（参考として掲載）。")
ws2["A2"].alignment = Alignment(wrap_text=True, vertical="top")
ws2.merge_cells("A2:I2")
ws2.row_dimensions[2].height = 46

H2 = ["システム", "アイテム名", "メーカー", "型番", "数量", "概算単価(円)", "小計(円)", "購入先", "備考"]

r2 = 4
ws2.cell(r2, 1, "A. 購入品（買い直し対象）").font = Font(bold=True, size=11)
r2 += 1
for c, h in enumerate(H2, 1):
    ws2.cell(r2, c, h)
style_header(ws2, r2, len(H2))
a_hdr = r2
r2 += 1

borrowed_rows = {5, 6, 7}
# セクションAは「推奨＝発送」の品目、つまり実際に日本から送る分に絞る。
sendable = [it for it in items
            if T[it["row"]][3] in (OK, COND) and T[it["row"]][0] != C4
            and it["row"] not in borrowed_rows]
sec_a = [it for it in sendable if "発送" in T[it["row"]][8]]
sec_b = [it for it in items if it["row"] in borrowed_rows]
# 持参推奨の品目。買い直し対象ではないが、見積の取りこぼしを防ぐため参考掲載する。
sec_d = [it for it in sendable if "発送" not in T[it["row"]][8]]

a_start = r2
for it in sec_a:
    note = it["note"] or ""
    if not it["model"] or it["model"] == "-":
        note = (note + " / 型番未確定：発注前に確定").strip(" /")
    if T[it["row"]][3] == COND:
        note = (note + " / 危険物申告が必要").strip(" /")
    vals = [it["system"], it["name"], it["maker"], it["model"], it["qty"],
            "要見積", None, None, note]
    for c, v in enumerate(vals, 1):
        cell = ws2.cell(r2, c, v)
        cell.border = BORDER
        cell.alignment = CTR if c in (5, 6) else WRAP
    ws2.cell(r2, 7, f"=IF(ISNUMBER(F{r2}),E{r2}*F{r2},\"\")").border = BORDER
    r2 += 1
a_end = r2 - 1
ws2.cell(r2, 4, "合計").font = Font(bold=True)
ws2.cell(r2, 7, f"=SUM(G{a_start}:G{a_end})").font = Font(bold=True)
ws2.cell(r2, 7).border = BORDER

r2 += 3
ws2.cell(r2, 1, "B. 借用品（太田研・荒井研）— 購入品ではないため買い直しの要否は要調整").font = Font(bold=True, size=11)
r2 += 1
for c, h in enumerate(H2, 1):
    ws2.cell(r2, c, h)
style_header(ws2, r2, len(H2))
r2 += 1
for it in sec_b:
    vals = [it["system"], it["name"], it["maker"], it["model"], it["qty"],
            "要見積", None, None, (it["note"] or "") + " / 借用品・手荷物で持参"]
    for c, v in enumerate(vals, 1):
        cell = ws2.cell(r2, c, v)
        cell.border = BORDER
        cell.alignment = CTR if c in (5, 6) else WRAP
    r2 += 1

r2 += 2
ws2.cell(r2, 1, "C. 現地調達を推奨（買い直し不要）").font = Font(bold=True, size=11)
r2 += 1
for it in items:
    if T[it["row"]][0] == C4 or "現地調達" in T[it["row"]][8]:
        ws2.cell(r2, 2, it["name"])
        ws2.cell(r2, 9, T[it["row"]][8])
        r2 += 1

r2 += 2
ws2.cell(r2, 1, "D. 手荷物で持参する品目（日本から送らないため買い直し対象外・参考掲載）").font = Font(bold=True, size=11)
r2 += 1
for c, h in enumerate(H2, 1):
    ws2.cell(r2, c, h)
style_header(ws2, r2, len(H2))
r2 += 1
for it in sec_d:
    vals = [it["system"], it["name"], it["maker"], it["model"], it["qty"],
            "—", None, None, ((it["note"] or "") + " / " + T[it["row"]][8]).strip(" /")]
    for c, v in enumerate(vals, 1):
        cell = ws2.cell(r2, c, v)
        cell.border = BORDER
        cell.alignment = CTR if c in (5, 6) else WRAP
    r2 += 1

for c, w in enumerate([12, 24, 13, 20, 7, 12, 12, 14, 30], 1):
    ws2.column_dimensions[get_column_letter(c)].width = w

# ================= シート3: 重要品目メモ =================
ws3 = wb.create_sheet("重要品目メモ", 3)
ws3.sheet_view.showGridLines = False
ws3.column_dimensions["A"].width = 4
ws3.column_dimensions["B"].width = 88

MEMO = [
    ("H1", "レーザーとマイクロ波アンプの機内持込 — 実現性の評価"),
    ("H2", "結論"),
    ("P", "どちらも「規則上は持込可能、実務上は保安検査の裁量が唯一の障壁」。"
          "レーザーは準備次第で通る見込み、マイクロ波アンプはほぼ問題なく通る。"
          "ただし乗継便のため、出発地・経由国・ドイツの3回の保安検査を通ることになり、"
          "経由国が最も読みにくい。"),
    ("H2", "1. レーザー（CNI MLL-S-532B-300mW）"),
    ("P", "・危険物該当性：DPSSレーザーでリチウム電池も高圧ガスも含まないため、IATA危険物規則上は該当なし。"
          "この点で持込を妨げる規則は存在しない。"),
    ("P", "・実際の障壁：保安検査官が「高出力レーザーポインタ」と判断した場合の任意放棄要求。"
          "300mW/532nm はクラス3Bにあたり、国によっては一般人の所持が規制される出力帯にあるため、"
          "研究機材であることをその場で示せるかが分かれ目になる。"),
    ("P", "・準備すべきもの（すべて英文・紙で携行）："),
    ("L", "所属機関発行の英文レター（研究目的、渡航先の受入機関名、機材名と型番を明記）"),
    ("L", "メーカー仕様書（波長・出力・クラス分類が読み取れるページ）"),
    ("L", "受入側（ドイツ）研究室からの受入証明またはメール printout"),
    ("L", "購入証明・インボイス（一時輸出であること、再輸入予定を示せると望ましい）"),
    ("L", "レーザー保護メガネ（研究用途であることの傍証になる）"),
    ("P", "・運用上のコツ：ヘッドと電源部を分けて機内持込バッグの取り出しやすい位置に入れ、"
          "検査時に自分から申告して書類を出す。黙って通そうとすると心証が悪く不利になる。"
          "また、機内での通電・動作は絶対にしないこと。"),
    ("P", "・輸出管理：別表第1第6項（貨物等省令第6条）の対象カテゴリ。CW 532nm・300mW は"
          "規制のしきい値を下回り非該当となる公算が大きいが、これは推定であり、"
          "CNI に該非判定書（パラメータシート）を必ず請求すること。手荷物での持出も輸出に当たるため、"
          "発送であれ持参であれ該非判定は同じく必要。"),
    ("P", "・実現性の評価：準備を揃えれば十分に現実的。ただし経由国での没収リスクはゼロにできないため、"
          "「万一失った場合に現地で代替をレンタル／購入できるか」を事前に受入研究室と確認しておくと安全。"),
    ("H2", "2. マイクロ波アンプ（Mini-Circuits ZHL-16W-43-S）"),
    ("P", "・危険物該当性：電池・ガスを含まない受動的な金属筐体の電子機器で、該当なし。"),
    ("P", "・実際の障壁：ほぼない。密度の高い金属塊なのでX線画像で不透明に写り、"
          "開披検査になる可能性は高いが、蓋を開けて仕様書を見せれば通る。重量2kg前後で手荷物重量制限内。"),
    ("P", "・準備すべきもの：メーカーのデータシート（英文）と、レーザーと同じ所属機関レター。"),
    ("P", "・輸出管理：別表第1第9項（貨物等省令第8条）。増幅器は動作周波数帯と出力によって該当し得るため、"
          "Mini-Circuits から該非判定書または ECCN の回答を取ること（要確認）。"),
    ("P", "・実現性の評価：高い。レーザーより優先度を下げてよい。"),
    ("H2", "3. 併せて注意すべき品目"),
    ("P", "・信号発生器 Windfreak SynthHD：広帯域シンセサイザで、本リスト中もっとも第9項該当のリスクが高い。"
          "輸送の可否ではなく輸出許可の要否が問題になるため、該非判定書の取得を最優先で進めること。"
          "該当すると判定された場合、発送・持参いずれでも経済産業大臣の許可が必要になり、"
          "取得に数週間かかるのでスケジュールに影響する。"),
    ("P", "・ベリリウム銅DAC：ベリリウム合金は含有率次第で別表第1第2項の対象になり得る。"
          "借用元に材質証明（Be含有率）を確認すること。"),
    ("P", "・アラルダイト：接着剤は硬化剤側が危険物に該当し得るため持込・預入とも不可。現地調達が現実的。"),
    ("P", "・電源電圧：日本の100V機（DC電源 PWR401L、はんだごて、LEDドライバー用電源）はドイツの230Vで"
          "そのまま使えない。変圧器を用意するか、現地で230V対応品を調達する方が結果的に安く済む場合がある。"),
    ("H2", "4. 免責"),
    ("P", "本メモの輸出令項番はいずれも公開仕様からの推定であり、該非判定の最終結論ではない。"
          "申請にあたっては各メーカーの該非判定書を取得し、所属機関の輸出管理担当部署の確認を受けること。"
          "航空会社の持込可否も最終的には当日の運送人および保安当局の判断による。"),
]

rr = 2
for kind, text in MEMO:
    cell = ws3.cell(rr, 2, ("・" + text) if kind == "L" else text)
    if kind == "H1":
        cell.font = Font(bold=True, size=14)
        rr += 1
    elif kind == "H2":
        cell.font = Font(bold=True, size=12, color="1F3864")
        rr += 1
    else:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    rr += 1

# ================= 印刷設定 =================
setup_print(wb["機材一覧"], header_row=4, paper="A3")  # 元の列幅が広いため A3
setup_print(ws, header_row=HR, paper="A3")      # 輸送分類（14列。A4では読めない大きさになる）
setup_print(ws2, header_row=None)               # 再購入リスト（セクション見出しが複数あるため繰り返し行なし）
setup_print(ws3, landscape=False, fit_width=True)  # 重要品目メモ（縦・文章主体）

wb.save(DST)
print("saved", DST)
print("items:", len(items), "sec_a:", len(sec_a), "sec_b:", len(sec_b), "sec_d:", len(sec_d))
from collections import Counter
print(Counter(T[i["row"]][0] for i in items))
print("要確認:", [i["name"] for i in items if T[i["row"]][6]])
