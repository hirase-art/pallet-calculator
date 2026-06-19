import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import japanize_matplotlib
import pandas as pd
import datetime
import io
import json
import re
import copy
import anthropic

# ==========================================
# 箱マスタ取得 (Google Sheets)
# ==========================================
@st.cache_data
def fetch_box_sizes():
    try:
        df = pd.read_csv("boxes.csv", dtype=str)
        boxes = {}
        for _, row in df.iterrows():
            try:
                maker    = str(row.iloc[0]).strip()
                box_type = str(row.iloc[1]).strip()
                l = int(float(row.iloc[2]))
                w = int(float(row.iloc[3]))
                h = int(float(row.iloc[4]))
                if maker in ("nan", "") and box_type in ("nan", ""):
                    continue
                label = f"{maker} / {box_type}" if maker not in ("nan", "") else box_type
                boxes[label] = {"L": l, "W": w, "H": h}
            except (ValueError, TypeError):
                continue
        return boxes
    except Exception as e:
        st.warning(f"箱マスタ取得エラー: {e}")
        return {}

# ページ設定 (ワイド表示)
st.set_page_config(page_title="Palletize Calculator", layout="wide")

# ==========================================
# 0. 簡易パスワード認証 (門番)
# ==========================================
def check_password():
    """Returns `True` if the user had the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if st.session_state["password"] == st.secrets["PASSWORD"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # パスワードをセッションから消す
        else:
            st.session_state["password_correct"] = False

    # 認証済みならTrueを返す
    if "password_correct" in st.session_state:
        if st.session_state["password_correct"]:
            return True

    # 未認証ならパスワード入力画面を出す
    st.text_input(
        "パスワードを入力してください", type="password", on_change=password_entered, key="password"
    )
    
    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("😕 パスワードが違います")
        
    return False

if not check_password():
    st.stop()  # パスワードが合うまで、これ以降の処理を止める

# ==========================================
# 1. UI & 入力エリア
# ==========================================
st.title("📦 Palletize Calculator")
st.markdown("箱のサイズと数量を入力して、最適なパレット積載プランを計算します。")

# サイドバー：基本設定
st.sidebar.header("基本設定")
PALLET_W = st.sidebar.number_input("パレット幅 (mm)", value=1100, step=10)
PALLET_D = st.sidebar.number_input("パレット奥行 (mm)", value=1100, step=10)
PALLET_H = st.sidebar.number_input("パレット高さ (mm)", value=150, step=10)
LIMIT_H  = st.sidebar.number_input("高さ制限 (mm)", value=1550, step=50)

# メインエリア：商品データ入力 (Data Editorを使用)
st.subheader("積載する商品リスト")

# デフォルトのデータフレーム
default_data = pd.DataFrame([
    {"Name": "Item-A", "L": 336, "W": 336, "H": 235, "QTY": 72, "Color": "#aaccff"},
    {"Name": "Item-B", "L": 503, "W": 363, "H": 321, "QTY": 13, "Color": "#ffcc99"},
])

# セッション状態の初期化
if "box_data" not in st.session_state:
    st.session_state.box_data = default_data.copy()
if "editor_key" not in st.session_state:
    st.session_state.editor_key = 0
if "coord_plan" not in st.session_state:
    st.session_state.coord_plan = None
if "coord_plan_history" not in st.session_state:
    st.session_state.coord_plan_history = []
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []

# 箱マスタから選択して追加
box_master = fetch_box_sizes()
if box_master:
    with st.expander("📋 箱マスタから選択して追加"):
        c1, c2, c3 = st.columns([4, 1, 1])
        with c1:
            selected_box = st.selectbox(
                "箱を選択", ["-- 選択してください --"] + list(box_master.keys()),
                key="box_selector"
            )
        with c2:
            add_qty = st.number_input("数量 (cs)", min_value=1, value=1, key="add_qty")
        with c3:
            add_color = st.color_picker("表示色", "#aaccff", key="add_color")

        if st.button("リストに追加", key="btn_add"):
            if selected_box != "-- 選択してください --":
                spec = box_master[selected_box]
                new_row = pd.DataFrame([{
                    "Name": selected_box,
                    "L": spec["L"], "W": spec["W"], "H": spec["H"],
                    "QTY": int(add_qty), "Color": add_color
                }])
                st.session_state.box_data = pd.concat(
                    [st.session_state.box_data, new_row], ignore_index=True
                )
                st.session_state.editor_key += 1
                st.rerun()
else:
    st.caption("⚠️ 箱マスタの読み込みに失敗しました。手動で入力してください。")

# 編集可能なテーブルを表示
edited_df = st.data_editor(
    st.session_state.box_data,
    key=f"box_editor_{st.session_state.editor_key}",
    num_rows="dynamic",
    column_config={
        "Name": "品名",
        "L": st.column_config.NumberColumn("長辺 (mm)", min_value=1, format="%d"),
        "W": st.column_config.NumberColumn("短辺 (mm)", min_value=1, format="%d"),
        "H": st.column_config.NumberColumn("高さ (mm)", min_value=1, format="%d"),
        "QTY": st.column_config.NumberColumn("数量 (cs)", min_value=1, format="%d"),
        "Color": "表示色",
    },
    use_container_width=True
)

# ==========================================
# 2. 計算ロジック (元のコードを移植)
# ==========================================

def get_best_layer_pattern(p_w, p_d, b_l, b_w):
    # パターン1: そのまま配置
    cols1 = p_w // b_l
    rows1 = p_d // b_w
    count1 = cols1 * rows1
    
    # パターン2: 90度回転
    cols2 = p_w // b_w
    rows2 = p_d // b_l
    count2 = cols2 * rows2
    
    if count1 >= count2:
        return {'count': count1, 'cols': cols1, 'rows': rows1, 
                'box_w_view': b_l, 'box_d_view': b_w, 'rotated': False}
    else:
        return {'count': count2, 'cols': cols2, 'rows': rows2, 
                'box_w_view': b_w, 'box_d_view': b_l, 'rotated': True}

def calculate_pallet_plan(input_data_dict, limit_h, pallet_h):
    all_layers_queue = []
    item_specs = {}
    
    for name, data in input_data_dict.items():
        pattern = get_best_layer_pattern(PALLET_W, PALLET_D, data['L'], data['W'])
        
        total_qty = data['QTY']
        if total_qty <= 0: continue 

        per_layer = pattern['count']
        if per_layer == 0: continue 

        full_layers = total_qty // per_layer
        remainder = total_qty % per_layer
        
        # 【修正箇所】ここに 'total_qty' を追加しました
        item_specs[name] = {
            'h': data['H'], 'color': data['Color'], 'pattern': pattern,
            'orig_l': data['L'], 'orig_w': data['W'],
            'total_qty': total_qty 
        }
        
        for _ in range(full_layers):
            all_layers_queue.append({'name': name, 'type': 'full', 'count': per_layer})
            
        if remainder > 0:
            all_layers_queue.append({'name': name, 'type': 'rem', 'count': remainder})

    pallets = []
    current_pallet = {'layers': [], 'current_h': pallet_h}
    
    for layer in all_layers_queue:
        name = layer['name']
        h = item_specs[name]['h']
        
        if current_pallet['current_h'] + h <= limit_h:
            current_pallet['layers'].append(layer)
            current_pallet['current_h'] += h
        else:
            pallets.append(current_pallet)
            current_pallet = {'layers': [layer], 'current_h': pallet_h + h}
            
    if current_pallet['layers']:
        pallets.append(current_pallet)
        
    return pallets, item_specs


# ==========================================
# Step 1: 座標ベースモデルへの変換
# ==========================================

def convert_to_coordinate_plan(pallets, item_specs, pallet_w, pallet_d, pallet_h):
    """
    段キュー形式の配置計画を、各箱に (x, y, z) 絶対座標を持つ形式に変換する。
    原点: x=0 はパレット左端、y=0 は手前端、z=0 は床面。
    箱グループはパレット上で中央揃え（既存の天面図描画と同じルール）。
    """
    coord_pallets = []

    for pallet_data in pallets:
        layers_out = []
        current_z = pallet_h  # パレット板の上面から積み始める

        for layer in pallet_data['layers']:
            name = layer['name']
            count = layer['count']
            spec = item_specs[name]
            pat = spec['pattern']

            box_l = pat['box_w_view']  # x 方向の寸法
            box_w = pat['box_d_view']  # y 方向の寸法
            box_h = spec['h']

            # ブロック全体をパレット中央に揃えるオフセット（既存天面図と一致）
            group_w = pat['cols'] * box_l
            group_d = pat['rows'] * box_w
            offset_x = (pallet_w - group_w) / 2
            offset_y = (pallet_d - group_d) / 2

            # 列優先 (col-major) で count 個だけ展開
            boxes = []
            for i in range(count):
                col = i // pat['rows']
                row = i % pat['rows']
                boxes.append({
                    'name': name,
                    'x': offset_x + col * box_l,
                    'y': offset_y + row * box_w,
                    'l': box_l,
                    'w': box_w,
                    'h': box_h,
                    'color': spec['color'],
                    'type': layer['type'],
                })

            layers_out.append({
                'layer_index': len(layers_out),
                'z_bottom': current_z,
                'height': box_h,
                'item_name': name,
                'type': layer['type'],
                'boxes': boxes,
            })
            current_z += box_h

        coord_pallets.append({
            'layers': layers_out,
            'total_height': current_z,
        })

    return coord_pallets


def validate_coord_plan(coord_pallets, pallet_w, pallet_d, limit_h):
    """配置の境界チェック。エラーメッセージのリストを返す。"""
    errors = []
    for pi, pallet in enumerate(coord_pallets):
        if pallet['total_height'] > limit_h:
            errors.append(
                f"PL#{pi+1}: 総高さ {pallet['total_height']}mm が制限 {limit_h}mm を超過"
            )
        for layer in pallet['layers']:
            for box in layer['boxes']:
                if box['x'] < 0 or box['x'] + box['l'] > pallet_w:
                    errors.append(
                        f"PL#{pi+1} {layer['layer_index']+1}段目 [{box['name']}]: "
                        f"x方向がパレット外 ({box['x']:.0f}~{box['x']+box['l']:.0f}mm, 上限{pallet_w}mm)"
                    )
                if box['y'] < 0 or box['y'] + box['w'] > pallet_d:
                    errors.append(
                        f"PL#{pi+1} {layer['layer_index']+1}段目 [{box['name']}]: "
                        f"y方向がパレット外 ({box['y']:.0f}~{box['y']+box['w']:.0f}mm, 上限{pallet_d}mm)"
                    )
    return errors


def create_figure(pallets, item_specs):
    n_pallets = len(pallets)
    n_items = len(item_specs)
    
    if n_pallets == 0:
        return None

    fig = plt.figure(figsize=(max(n_pallets*4, 8), 10))
    gs = fig.add_gridspec(2, max(n_pallets, n_items), height_ratios=[1, 2.5])
    
    # --- A. 天面図 (Top View) の修正 ---
    col_idx = 0
    for name, spec in item_specs.items():
        ax = fig.add_subplot(gs[0, col_idx])
        ax.set_title(f"{name}\n({spec['orig_l']}x{spec['orig_w']}mm)", fontsize=10)
        ax.set_xlim(0, 1200); ax.set_ylim(0, 1200)
        ax.set_aspect('equal')
        ax.axis('off')
        
        ax.add_patch(patches.Rectangle((50, 50), 1100, 1100, fill=False, edgecolor='black', lw=2))
        
        pat = spec['pattern']
        box_w = pat['box_w_view']
        box_d = pat['box_d_view']
        
        total_w = pat['cols'] * box_w
        total_d = pat['rows'] * box_d
        start_x = 50 + (1100 - total_w) / 2
        start_y = 50 + (1100 - total_d) / 2
        
        # 【修正】総数量を超えたら描画を止めるカウンター
        drawn_count = 0
        total_item_qty = spec['total_qty']

        for c in range(pat['cols']):
            for r in range(pat['rows']):
                # もし「これ以上箱がない」ならループを抜ける
                if drawn_count >= total_item_qty:
                    break
                
                ax.add_patch(patches.Rectangle(
                    (start_x + c*box_w, start_y + r*box_d), 
                    box_w, box_d, 
                    facecolor=spec['color'], edgecolor='black', lw=1, alpha=0.7
                ))
                drawn_count += 1
        
        info_txt = f"{pat['cols']}x{pat['rows']}={pat['count']}cs/段"
        if pat['rotated']: info_txt += "\n(90°回転)"
        ax.text(600, 0, info_txt, ha='center', va='top', fontsize=9)
        col_idx += 1

    # --- B. 側面図 (Side View) の修正 ---
    for i, pallet in enumerate(pallets):
        ax = fig.add_subplot(gs[1, i])
        ax.set_title(f"Pallet #{i+1}", fontsize=12, fontweight='bold')
        ax.set_xlim(0, 1400)
        ax.set_ylim(0, 1800)
        ax.axis('off')
        
        ax.axhline(y=0, color='black', lw=2)
        ax.add_patch(patches.Rectangle((150, 0), 1100, PALLET_H, facecolor='#8B4513', edgecolor='black'))
        
        current_h = PALLET_H
        
        for layer in pallet['layers']:
            name = layer['name']
            spec = item_specs[name]
            h = spec['h']
            count = layer['count'] # その段にある実際の個数
            is_rem = (layer['type'] == 'rem')
            
            cols = spec['pattern']['cols']
            box_vis_w = spec['pattern']['box_w_view']
            layer_w = cols * box_vis_w
            start_x = 150 + (1100 - layer_w) / 2
            
            if is_rem:
                edge_col = 'red'
                line_sty = '--'
                alpha_val = 0.4
                line_w = 1.5
                text_col = 'red'
                label = f"{name}\n(端数: {count})"
            else:
                edge_col = 'black'
                line_sty = '-'
                alpha_val = 1.0
                line_w = 0.5
                text_col = 'black'
                label = f"{name}"

            # 【修正】側面から見える箱の数を制限する
            # パレットの幅方向に並ぶ最大数(cols)と、実際の残数(count)のうち、少ない方だけ描画する
            visible_boxes = min(cols, count)
            
            for c in range(visible_boxes):
                ax.add_patch(patches.Rectangle(
                    (start_x + c*box_vis_w, current_h), box_vis_w, h, 
                    facecolor=spec['color'], edgecolor=edge_col, 
                    linestyle=line_sty, linewidth=line_w, alpha=alpha_val
                ))
            
            ax.text(700, current_h + h/2, label, ha='center', va='center', fontsize=8, color=text_col, fontweight='bold' if is_rem else 'normal')

            current_h += h
            
        ax.text(700, current_h + 30, f"H: {current_h}mm", ha='center', fontweight='bold')
        ax.axhline(y=LIMIT_H, color='red', linestyle='--', lw=1)
        ax.text(1350, LIMIT_H, "Limit", color='red', va='bottom', ha='right', fontsize=8)

    plt.tight_layout()
    return fig


# ==========================================
# Step 2: 座標モデルからの新描画関数
# ==========================================

def create_layer_topviews(coord_plan, pallet_w, pallet_d):
    """各パレット × 各段の天面図グリッドを生成する（coord_plan ベース）"""
    n_pallets = len(coord_plan)
    max_layers = max(len(p['layers']) for p in coord_plan)
    if max_layers == 0:
        return None

    cell_w = max(2.2, min(3.0, 18.0 / max_layers))
    cell_h = 2.8
    fig, axes = plt.subplots(
        n_pallets, max_layers,
        figsize=(max_layers * cell_w, n_pallets * cell_h),
        squeeze=False,
    )

    for pi, pallet in enumerate(coord_plan):
        for li in range(max_layers):
            ax = axes[pi][li]
            ax.set_xlim(-20, pallet_w + 20)
            ax.set_ylim(-25, pallet_d + 20)
            ax.set_aspect('equal')
            ax.axis('off')

            if li < len(pallet['layers']):
                layer = pallet['layers'][li]
                t_color = 'red' if layer['type'] == 'rem' else 'black'
                ax.set_title(f"PL#{pi+1}  {li+1}段目", fontsize=8, color=t_color, pad=2)

                # パレット外枠
                ax.add_patch(patches.Rectangle(
                    (0, 0), pallet_w, pallet_d,
                    fill=False, edgecolor='#777777', lw=1.5,
                ))

                for box in layer['boxes']:
                    edge = 'red' if box['type'] == 'rem' else '#222222'
                    alpha = 0.5 if box['type'] == 'rem' else 0.75
                    ax.add_patch(patches.Rectangle(
                        (box['x'], box['y']), box['l'], box['w'],
                        facecolor=box['color'], edgecolor=edge,
                        linewidth=0.5, alpha=alpha,
                    ))

                ax.text(
                    pallet_w / 2, -15,
                    f"{len(layer['boxes'])}cs",
                    ha='center', va='top', fontsize=7,
                )
            else:
                ax.set_visible(False)

    fig.suptitle("天面図（段別）", fontsize=10)
    plt.tight_layout()
    return fig


def create_side_views_v2(coord_plan, pallet_w, pallet_h, limit_h):
    """coord_plan から側面図（x-z 平面）を生成する"""
    n_pallets = len(coord_plan)
    if n_pallets == 0:
        return None

    fig, axes = plt.subplots(
        1, n_pallets,
        figsize=(max(n_pallets * 4, 8), 8),
        squeeze=False,
    )

    view_offset = 150  # パレット左端のビュー x 座標

    for pi, (pallet, ax) in enumerate(zip(coord_plan, axes[0])):
        ax.set_title(f"Pallet #{pi+1}", fontsize=12, fontweight='bold')
        ax.set_xlim(0, view_offset + pallet_w + view_offset)
        ax.set_ylim(0, 1800)
        ax.axis('off')

        ax.axhline(y=0, color='black', lw=2)
        ax.add_patch(patches.Rectangle(
            (view_offset, 0), pallet_w, pallet_h,
            facecolor='#8B4513', edgecolor='black',
        ))

        for layer in pallet['layers']:
            z = layer['z_bottom']
            h = layer['height']
            is_rem = layer['type'] == 'rem'

            edge_col = 'red' if is_rem else 'black'
            line_sty = '--' if is_rem else '-'
            alpha_val = 0.4 if is_rem else 1.0
            line_w = 1.5 if is_rem else 0.5
            text_col = 'red' if is_rem else 'black'

            # y が大きい箱（奥）から先に描き、手前 (y 小) を前面に出す
            for box in sorted(layer['boxes'], key=lambda b: -b['y']):
                ax.add_patch(patches.Rectangle(
                    (view_offset + box['x'], z), box['l'], h,
                    facecolor=box['color'], edgecolor=edge_col,
                    linestyle=line_sty, linewidth=line_w, alpha=alpha_val,
                ))

            label = layer['item_name'] + (" (端数)" if is_rem else "")
            cx = view_offset + pallet_w / 2
            ax.text(cx, z + h / 2, label,
                    ha='center', va='center', fontsize=8,
                    color=text_col, fontweight='bold' if is_rem else 'normal')

        total_h = pallet['total_height']
        cx = view_offset + pallet_w / 2
        ax.text(cx, total_h + 30, f"H: {total_h}mm", ha='center', fontweight='bold')
        ax.axhline(y=limit_h, color='red', linestyle='--', lw=1)
        ax.text(view_offset + pallet_w + 40, limit_h, "Limit",
                color='red', va='bottom', ha='left', fontsize=8)

    plt.tight_layout()
    return fig


# ==========================================
# Step 3: Claude Opus 連携
# ==========================================

def recompute_z_bottoms(coord_plan, pallet_h):
    """Claude が段を並べ替えた後に z_bottom を再計算する"""
    for pallet in coord_plan:
        z = pallet_h
        for layer in pallet['layers']:
            layer['z_bottom'] = z
            layer['layer_index'] = pallet['layers'].index(layer)
            for box in layer['boxes']:
                box['h'] = layer['height']
            z += layer['height']
        pallet['total_height'] = z
    return coord_plan


# 積付パターン定義辞書（座標モデル向け変換ルール）
PALLETIZING_PATTERNS = """
【積付パターン定義】
箱グループはパレット上で常に中央揃え:
  offset_x = (pallet_w - cols * box_l) / 2
  offset_y = (pallet_d - rows * box_w) / 2
列優先(col-major)で展開: i番目の箱 → col = i // rows, row = i % rows

■ ブロック積み (Block Stacking)
  全段: 全箱を同一方向。cols/rows/向きは段をまたいで変えない。

■ 交互列積み (Interlock / Cross Stacking)
  layer_index 偶数段(0,2,4...): 元の向き (l=orig_l, w=orig_w)
    cols_A = pallet_w // orig_l, rows_A = pallet_d // orig_w
  layer_index 奇数段(1,3,5...): 90度回転 (l=orig_w, w=orig_l)
    cols_B = pallet_w // orig_w, rows_B = pallet_d // orig_l
  各段で offset を再計算して中央揃え。
  条件: cols_B≥1 かつ rows_B≥1 であること。

■ レンガ積み (Brick Stacking)
  各段内: 左 half_cols 列を縦向き(l=orig_l)、残りを横向き(l=orig_w)で混在。
  段ごとに左右反転(180度): col → cols-1-col, row → rows-1-row。
  外観: 壁のレンガ模様。安定性高く検品しやすい。倉庫納品標準。

■ ピンホール積み / 風車積み (Pinhole / Pinwheel Stacking)
  用途: 冷蔵・冷凍品の通気確保。中央に縦貫通気孔を設ける。

  デフォルト動作（指示がない場合はこれを適用）:
    1. 通常の cols×rows グリッドを算出。
    2. 以下の「中央除外ゾーン」に該当する箱を配置しない:
         center_cols = { cols//2 }              # cols が奇数の場合
         center_cols = { cols//2-1, cols//2 }   # cols が偶数の場合
         center_rows = { rows//2 }              # rows が奇数の場合
         center_rows = { rows//2-1, rows//2 }   # rows が偶数の場合
       除外条件: col in center_cols AND row in center_rows
    3. 段ごとに180度反転(col→cols-1-col, row→rows-1-row)。
       通気孔は中央対称なため反転後も同位置 → 全段で縦貫通気孔を維持。

  具体例(3×3グリッド, 除外1箱):
    偶数段: (0,0)(1,0)(2,0)(0,1)除外(2,1)(0,2)(1,2)(2,2) → 8箱
    奇数段: 同上を180度反転 → 8箱(通気孔は中央のまま)

  ユーザー指示で調整可能:
    「通気孔を大きくして」  → center_cols/center_rows の除外範囲を各1列/行拡大
    「通気孔を2列にして」  → center_cols を 2列に指定
    「縦通気孔を1列にして」→ center_cols を 1列に制限

■ スプリット積み (Split Stacking)
  レンガ積みの変形。横向き列の内側に隙間を設けパレット外形を合わせる。
  外周フラット、隙間は内部。寸法が合わない品に使用。

■ 窓積み (Window Stacking)
  横向き列を2列配置し全箱が外から視認できる。検品最優先。
"""


def _extract_json_object(text: str) -> str:
    """
    テキスト中から最初の { ～ 対応する } を抽出する。
    Claude が JSON の前後に説明文を付けた場合や、コードブロックが混在する場合に対応。
    """
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def call_claude_for_pallet(coord_plan, user_instruction, pallet_w, pallet_d, pallet_h, limit_h):
    """
    Claude Opus に積付変更を依頼する。
    戻り値: (new_plan, explanation, warnings)
    エラー時: (None, error_message, [])
    """
    try:
        client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    except Exception as e:
        return None, f"ANTHROPIC_API_KEY が未設定です: {e}", []

    plan_json = json.dumps(coord_plan, ensure_ascii=False)

    system_prompt = f"""あなたはパレタイズの専門家AIです。
ユーザーの指示に従い、パレット積付配置を修正してください。

【座標系】
- x: パレット幅方向（左端=0、右端={pallet_w}mm）
- y: パレット奥行方向（手前=0、奥端={pallet_d}mm）
- z: 高さ方向（床=0、パレット板上面={pallet_h}mmから積み始め）

【制約】
- 各箱は 0 ≤ x+l ≤ {pallet_w}、0 ≤ y+w ≤ {pallet_d} を守る
- 同一段内で箱がxy平面上に重ならない
- 各パレットの total_height ≤ {limit_h}mm

【coord_plan の構造】
pallets[] → layers[] → boxes[]
  layer: layer_index(0始まり), z_bottom(mm), height(mm), item_name, type("full"/"rem")
  box: name, x, y, l, w, h, color, type

{PALLETIZING_PATTERNS}

【出力形式】
JSONのみ返してください。マークダウン不要。
{{"modified_plan": <変更後の完全なcoord_plan>, "explanation": "変更内容の説明", "warnings": []}}"""

    user_message = f"現在の配置:\n{plan_json}\n\n指示: {user_instruction}"

    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=32000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()

        # テキスト中から最初の { ～ 最後の対応する } を抽出
        # （Claudeが説明文をJSONの前後に付けた場合もこれで対処）
        extracted = _extract_json_object(raw)
        if not extracted:
            preview = raw[:300] if raw else "(空のレスポンス)"
            return None, f"JSONが見つかりませんでした。\n\nClaude の出力（先頭300文字）:\n{preview}", []

        data = json.loads(extracted)
        new_plan = data["modified_plan"]
        new_plan = recompute_z_bottoms(new_plan, pallet_h)
        explanation = data.get("explanation", "修正しました")
        warnings = data.get("warnings", [])
        return new_plan, explanation, warnings

    except json.JSONDecodeError as e:
        preview = extracted[:300] if "extracted" in dir() else raw[:300]
        return None, f"JSON解析エラー: {e}\n\n解析対象（先頭300文字）:\n{preview}", []
    except KeyError as e:
        return None, f"レスポンス構造エラー: {e}", []
    except Exception as e:
        return None, f"API呼び出しエラー: {e}", []


# ==========================================
# 3. 実行ボタン & 結果表示
# ==========================================

if st.button("計算して描画する", type="primary"):
    
    # DataFrameを辞書形式に変換 (元のロジックに合わせる)
    input_data_dict = {}
    for index, row in edited_df.iterrows():
        if row["Name"] and row["QTY"] > 0: # 空行対策
            input_data_dict[row["Name"]] = {
                'L': int(row["L"]),
                'W': int(row["W"]),
                'H': int(row["H"]),
                'QTY': int(row["QTY"]),
                'Color': row["Color"]
            }
    
    if not input_data_dict:
        st.error("有効なデータがありません。数値を入力してください。")
    else:
        # 計算実行
        pallets, item_specs = calculate_pallet_plan(input_data_dict, LIMIT_H, PALLET_H)

        # ─── Step 1: 座標モデルへ変換・検証 ───────────────────────────
        coord_plan = convert_to_coordinate_plan(pallets, item_specs, PALLET_W, PALLET_D, PALLET_H)
        st.session_state.coord_plan = coord_plan
        validation_errors = validate_coord_plan(coord_plan, PALLET_W, PALLET_D, LIMIT_H)

        with st.expander("🔍 座標モデル確認（Step 1 検証）", expanded=True):
            if validation_errors:
                for err in validation_errors:
                    st.error(err)
            else:
                st.success("✅ 全箱がパレット範囲内に収まっています")

            # 段別サマリーテーブル
            summary_rows = []
            for pi, pallet in enumerate(coord_plan):
                for layer in pallet['layers']:
                    summary_rows.append({
                        'PL': f"PL#{pi+1}",
                        '段': layer['layer_index'] + 1,
                        '品目': layer['item_name'],
                        '種別': '満載' if layer['type'] == 'full' else '⚠️端数',
                        '箱数': len(layer['boxes']),
                        'z底面(mm)': int(layer['z_bottom']),
                        '段高(mm)': int(layer['height']),
                        'z天面(mm)': int(layer['z_bottom'] + layer['height']),
                    })
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            # 品目別合計チェック（入力数量 vs 配置数）
            st.caption("品目別 配置箱数チェック")
            placed_counts = {}
            for pallet in coord_plan:
                for layer in pallet['layers']:
                    n = layer['item_name']
                    placed_counts[n] = placed_counts.get(n, 0) + len(layer['boxes'])

            check_rows = []
            for name, spec in item_specs.items():
                placed = placed_counts.get(name, 0)
                check_rows.append({
                    '品目': name,
                    '入力数量': spec['total_qty'],
                    '配置数': placed,
                    '一致': '✅' if placed == spec['total_qty'] else '❌ 不一致',
                })
            st.dataframe(pd.DataFrame(check_rows), use_container_width=True, hide_index=True)
        # ──────────────────────────────────────────────────────────────
        # 図・指示書・チャットはボタンブロック外のセクションで描画する
        # 新規計算時にチャット履歴をリセット
        st.session_state.chat_messages = []
        st.session_state.coord_plan_history = []


# ==========================================
# 4. 永続表示セクション（計算後・チャット後どちらでも再描画）
# ==========================================

if st.session_state.coord_plan is not None:
    coord_plan = st.session_state.coord_plan

    # ─ 天面図（段別）＋側面図 ─────────────────────────────────────────
    fig_top = create_layer_topviews(coord_plan, PALLET_W, PALLET_D)
    fig_side = create_side_views_v2(coord_plan, PALLET_W, PALLET_H, LIMIT_H)

    if fig_top:
        st.pyplot(fig_top)
        plt.close(fig_top)
    if fig_side:
        st.pyplot(fig_side)
        fn = f"pallet_plan_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.png"
        img_buf = io.BytesIO()
        fig_side.savefig(img_buf, format='png', dpi=150)
        st.download_button(
            label="画像をダウンロード（側面図）",
            data=img_buf, file_name=fn, mime="image/png",
        )
        plt.close(fig_side)

    # ─ 積付指示書 ────────────────────────────────────────────────────
    st.divider()
    st.subheader("📝 積付指示書")
    inst_cols = st.columns(len(coord_plan))
    for pi, (pallet, col) in enumerate(zip(coord_plan, inst_cols)):
        with col:
            st.markdown(f"**Pallet #{pi+1}**")
            st.caption(f"総高さ: {pallet['total_height']}mm")
            layers_data = []
            for layer in reversed(pallet['layers']):
                l_type = "満載" if layer['type'] == 'full' else "⚠️端数"
                layers_data.append({
                    "品目": layer['item_name'],
                    "数量": f"{len(layer['boxes'])}cs",
                    "状態": l_type,
                })
            st.table(layers_data)

    # ─ Claude チャット ────────────────────────────────────────────────
    st.divider()
    st.subheader("💬 積付指示 (Claude Opus)")
    st.caption("例：「交互列積みにする」「1PL目3段目に端数のBを入れ込む」「PL4をPL3に統合する」")

    # 元に戻すボタン（履歴があるときだけ表示）
    if st.session_state.coord_plan_history:
        if st.button("↩ 元に戻す"):
            st.session_state.coord_plan = st.session_state.coord_plan_history.pop()
            st.session_state.chat_messages.append({
                "role": "assistant",
                "content": "前の配置に戻しました。",
                "warnings": [],
            })
            st.rerun()

    # チャット履歴表示
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            for w in msg.get("warnings", []):
                st.warning(w)

    # チャット入力
    if instruction := st.chat_input("積付指示を入力..."):
        st.session_state.chat_messages.append({"role": "user", "content": instruction})

        with st.spinner("Claude が配置を検討中..."):
            new_plan, explanation, warnings = call_claude_for_pallet(
                st.session_state.coord_plan,
                instruction,
                PALLET_W, PALLET_D, PALLET_H, LIMIT_H,
            )

        if new_plan is not None:
            st.session_state.coord_plan_history.append(
                copy.deepcopy(st.session_state.coord_plan)
            )
            st.session_state.coord_plan = new_plan
            # バリデーションエラーがあれば警告に追記
            extra = validate_coord_plan(new_plan, PALLET_W, PALLET_D, LIMIT_H)
            warnings.extend(extra)
        else:
            explanation = f"❌ {explanation}"
            warnings = []

        st.session_state.chat_messages.append({
            "role": "assistant",
            "content": explanation,
            "warnings": warnings,
        })
        st.rerun()