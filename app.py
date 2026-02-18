import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import pandas as pd
import datetime
import io

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

# 編集可能なテーブルを表示
edited_df = st.data_editor(
    default_data,
    num_rows="dynamic", # 行の追加削除を許可
    column_config={
        "Name": "品名",
        "L": st.column_config.NumberColumn("長辺 (mm)", min_value=1, format="%d"),
        "W": st.column_config.NumberColumn("短辺 (mm)", min_value=1, format="%d"),
        "H": st.column_config.NumberColumn("高さ (mm)", min_value=1, format="%d"),
        "QTY": st.column_config.NumberColumn("数量 (cs)", min_value=1, format="%d"),
        "Color": "表示色", # 文字入力
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
        if total_qty <= 0: continue # 数量0はスキップ

        per_layer = pattern['count']
        if per_layer == 0: continue # パレットに乗らないサイズ

        full_layers = total_qty // per_layer
        remainder = total_qty % per_layer
        
        item_specs[name] = {
            'h': data['H'], 'color': data['Color'], 'pattern': pattern,
            'orig_l': data['L'], 'orig_w': data['W']
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

def create_figure(pallets, item_specs):
    n_pallets = len(pallets)
    n_items = len(item_specs)
    
    if n_pallets == 0:
        return None

    # Figure設定
    fig = plt.figure(figsize=(max(n_pallets*4, 8), 10)) # 少し幅広に調整
    gs = fig.add_gridspec(2, max(n_pallets, n_items), height_ratios=[1, 2.5])
    
    # --- A. 天面図 ---
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
        
        for c in range(pat['cols']):
            for r in range(pat['rows']):
                ax.add_patch(patches.Rectangle(
                    (start_x + c*box_w, start_y + r*box_d), 
                    box_w, box_d, 
                    facecolor=spec['color'], edgecolor='black', lw=1, alpha=0.7
                ))
        
        info_txt = f"{pat['cols']}x{pat['rows']}={pat['count']}cs/段"
        if pat['rotated']: info_txt += "\n(90°回転)"
        ax.text(600, 0, info_txt, ha='center', va='top', fontsize=9)
        col_idx += 1

# --- B. 側面図 ---
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
            count = layer['count']
            is_rem = (layer['type'] == 'rem')
            
            cols = spec['pattern']['cols']
            box_vis_w = spec['pattern']['box_w_view']
            layer_w = cols * box_vis_w
            start_x = 150 + (1100 - layer_w) / 2
            
            # 端数か満載かでスタイルを変える
            if is_rem:
                edge_col = 'red'
                line_sty = '--'
                alpha_val = 0.4 # 少し薄くする
                line_w = 1.5
                text_col = 'red'
                label = f"{name}\n(端数: {count})"
                # 端数エリア全体の枠も描画（任意）
                # ax.add_patch(patches.Rectangle((150, current_h), 1100, h, fill=False, edgecolor='red', linestyle=':', alpha=0.3))
            else:
                edge_col = 'black'
                line_sty = '-'
                alpha_val = 1.0 # くっきり
                line_w = 0.5
                text_col = 'black'
                label = f"{name}"

            # 箱を1つずつ描画（端数でも箱の形を見せる）
            for c in range(cols):
                ax.add_patch(patches.Rectangle(
                    (start_x + c*box_vis_w, current_h), box_vis_w, h, 
                    facecolor=spec['color'], edgecolor=edge_col, 
                    linestyle=line_sty, linewidth=line_w, alpha=alpha_val
                ))
            
            # テキスト表示
            ax.text(700, current_h + h/2, label, ha='center', va='center', fontsize=8, color=text_col, fontweight='bold' if is_rem else 'normal')

            current_h += h
            
        ax.text(700, current_h + 30, f"H: {current_h}mm", ha='center', fontweight='bold')
        ax.axhline(y=LIMIT_H, color='red', linestyle='--', lw=1)
        ax.text(1350, LIMIT_H, "Limit", color='red', va='bottom', ha='right', fontsize=8)

    plt.tight_layout()
    return fig

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
        
        # 1. グラフ描画
        fig = create_figure(pallets, item_specs)
        if fig:
            st.pyplot(fig)
            
            # 画像ダウンロードボタン
            fn = f"pallet_plan_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.png"
            img_buf = io.BytesIO()
            fig.savefig(img_buf, format='png', dpi=150)
            st.download_button(label="画像をダウンロード", data=img_buf, file_name=fn, mime="image/png")

        # 2. テキスト指示書
        st.divider()
        st.subheader("📝 積付指示書")
        
        # Streamlitのカラム機能で見やすく表示
        cols = st.columns(len(pallets))
        
        for i, pallet in enumerate(pallets):
            with cols[i]:
                st.markdown(f"**Pallet #{i+1}**")
                st.caption(f"総高さ: {pallet['current_h']}mm")
                
                # データフレームで見やすく表示するためのリスト作成
                layers_data = []
                for layer in reversed(pallet['layers']):
                    l_type = "満載" if layer['type'] == 'full' else "⚠️端数"
                    layers_data.append({
                        "品目": layer['name'],
                        "数量": f"{layer['count']}cs",
                        "状態": l_type
                    })
                
                st.table(layers_data)
