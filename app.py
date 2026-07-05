from flask import Flask, render_template, request, redirect, url_for, flash
import numpy as np
from pymongo import MongoClient
from bson.objectid import ObjectId
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os
import requests
from datetime import datetime

app = Flask(__name__)
app.secret_key = "ahp_secret_key"
app.jinja_env.globals.update(enumerate=enumerate)

# Kết nối MongoDB
client = MongoClient('mongodb://localhost:27017/')
db = client['ahp_investment_db']
criteria_collection = db['criteria']
alternatives_collection = db['alternatives']
comparisons_collection = db['pairwise_comparisons']
results_collection = db['results']

# Thư mục lưu biểu đồ
CHART_DIR = os.path.join('static', 'charts')
if not os.path.exists(CHART_DIR):
    os.makedirs(CHART_DIR)

ALPHA_VANTAGE_API_KEY = "4LGB7YK3L6EJYCPR"  

# Hàm tính toán AHP
def calculate_weights(matrix):
    col_sums = matrix.sum(axis=0)
    normalized_matrix = matrix / col_sums
    return normalized_matrix.mean(axis=1)

def check_consistency(matrix, weights):
    n = matrix.shape[0]
    lambda_max = sum((matrix @ weights) / weights) / n
    CI = (lambda_max - n) / (n - 1)
    RI = [0, 0, 0.58, 0.9, 1.12, 1.24, 1.32, 1.41, 1.45][n-1]
    return CI / RI if RI > 0 else 0

# Hàm lấy dữ liệu tài chính từ Alpha Vantage
def fetch_financial_data(symbols):
    try:
        prices = {}
        for symbol in symbols:
            url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&apikey={ALPHA_VANTAGE_API_KEY}"
            response = requests.get(url).json()
            if "Time Series (Daily)" in response:
                latest_date = sorted(response["Time Series (Daily)"].keys())[0]
                prices[symbol] = float(response["Time Series (Daily)"][latest_date]["4. close"])
            else:
                prices[symbol] = 0  # Nếu không lấy được, mặc định 0
        return prices
    except Exception as e:
        return None

# Trang chính
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'add_criteria' in request.form:
            criteria = request.form['criteria'].strip()
            if criteria:
                criteria_collection.insert_one({"name": criteria})
                flash("Tiêu chí đã được thêm!")
        elif 'add_alternative' in request.form:
            alternative = request.form['alternative'].strip()
            if alternative:
                alternatives_collection.insert_one({"name": alternative})
                flash("Phương án đã được thêm!")
        elif 'delete_criteria' in request.form:
            crit_id = request.form['crit_id']
            crit_name = request.form['crit_name']
            criteria_collection.delete_one({"_id": ObjectId(crit_id)})
            comparisons_collection.delete_many({"criteria_name": crit_name})
            flash("Tiêu chí đã được xóa! Vui lòng cập nhật ma trận liên quan.")
        elif 'delete_alternative' in request.form:
            alt_id = request.form['alt_id']
            alternatives_collection.delete_one({"_id": ObjectId(alt_id)})
            flash("Phương án đã được xóa! Vui lòng cập nhật ma trận liên quan.")
        elif 'delete_result' in request.form:
            result_id = request.form['result_id']
            results_collection.delete_one({"_id": ObjectId(result_id)})
            flash("Kết quả đã được xóa!")
    
    criteria = list(criteria_collection.find())
    alternatives = list(alternatives_collection.find())
    results = list(results_collection.find().sort("timestamp", -1))
    return render_template('index.html', criteria=criteria, alternatives=alternatives, results=results)

# Trang nhập ma trận
@app.route('/matrix/<type>/<name>', methods=['GET', 'POST'])
def matrix(type, name):
    if type == 'criteria':
        items = list(criteria_collection.find())
    else:
        items = list(alternatives_collection.find())
    
    n = len(items)
    if n < 2:
        flash("Cần ít nhất 2 mục để so sánh!")
        return redirect(url_for('index'))
    
    item_names = [item['name'] for item in items]
    existing_matrix = comparisons_collection.find_one({"type": type, "criteria_name": name if type == 'alternatives' else None})
    if existing_matrix and len(existing_matrix['matrix']) == n:
        matrix = np.array(existing_matrix['matrix'])
    else:
        matrix = np.ones((n, n))
    
    if request.method == 'POST':
        try:
            if 'suggest' in request.form:
                if type == 'criteria' and n == 4:
                    matrix = SAMPLE_CRITERIA_MATRIX
                elif type == 'alternatives' and n == 5 and name in SAMPLE_ALT_MATRICES:
                    matrix = SAMPLE_ALT_MATRICES[name]
            elif 'fetch_api' in request.form:
                prices = fetch_financial_data(item_names)
                if prices:
                    for i in range(n):
                        for j in range(i + 1, n):
                            if prices[item_names[i]] and prices[item_names[j]]:
                                matrix[i][j] = round(prices[item_names[i]] / prices[item_names[j]], 2)
                                matrix[j][i] = round(1 / matrix[i][j], 2)
                else:
                    flash("Không thể lấy dữ liệu từ API!")
            else:
                for i in range(n):
                    for j in range(i + 1, n):
                        key = f"{i}_{j}"
                        value = request.form.get(key, '1')
                        try:
                            matrix[i][j] = float(value)
                            matrix[j][i] = 1 / float(value)
                        except ValueError:
                            flash(f"Giá trị không hợp lệ tại {item_names[i]} vs {item_names[j]}!")
                            return render_template('matrix.html', type=type, name=name, items=item_names, matrix=matrix.tolist())
                
                weights = calculate_weights(matrix)
                cr = check_consistency(matrix, weights)
                
                if cr >= 0.1:
                    flash(f"Ma trận không nhất quán (CR = {cr:.4f} >= 0.1). Vui lòng đánh giá lại!")
                    return render_template('matrix.html', type=type, name=name, items=item_names, matrix=matrix.tolist())
                
                comparisons_collection.update_one(
                    {"type": type, "criteria_name": name if type == 'alternatives' else None},
                    {
                        "$set": {
                            "matrix": matrix.tolist(),
                            "weights": weights.tolist(),
                            "consistency_ratio": float(cr),
                            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        }
                    },
                    upsert=True
                )
                flash("Ma trận đã được lưu!")
                return redirect(url_for('index'))
        except Exception as e:
            flash(f"Lỗi khi lưu ma trận: {str(e)}")
            return render_template('matrix.html', type=type, name=name, items=item_names, matrix=matrix.tolist())
    
    return render_template('matrix.html', type=type, name=name, items=item_names, matrix=matrix.tolist())

# Tính toán và hiển thị kết quả
@app.route('/calculate', methods=['POST'])
def calculate():
    criteria = list(criteria_collection.find())
    alternatives = list(alternatives_collection.find())
    
    if len(criteria) < 1 or len(alternatives) < 2:
        flash("Vui lòng thêm tiêu chí và ít nhất 2 phương án!")
        return redirect(url_for('index'))
    
    criteria_matrix_doc = comparisons_collection.find_one({"type": "criteria"})
    if not criteria_matrix_doc:
        flash("Vui lòng nhập ma trận so sánh tiêu chí!")
        return redirect(url_for('index'))
    
    if len(criteria_matrix_doc['weights']) != len(criteria):
        flash("Kích thước ma trận tiêu chí không khớp với số tiêu chí hiện tại. Vui lòng cập nhật ma trận!")
        return redirect(url_for('index'))
    
    for crit in criteria:
        alt_matrix_doc = comparisons_collection.find_one({"type": "alternatives", "criteria_name": crit['name']})
        if not alt_matrix_doc:
            flash(f"Vui lòng nhập ma trận phương án cho {crit['name']}!")
            return redirect(url_for('index'))
        if len(alt_matrix_doc['matrix']) != len(alternatives):
            flash(f"Ma trận phương án cho {crit['name']} không khớp với số phương án hiện tại. Vui lòng cập nhật!")
            return redirect(url_for('index'))
    
    criteria_weights = np.array(criteria_matrix_doc['weights'])
    final_scores = np.zeros(len(alternatives))
    for i, crit in enumerate(criteria):
        alt_matrix_doc = comparisons_collection.find_one({"type": "alternatives", "criteria_name": crit['name']})
        alt_weights = calculate_weights(np.array(alt_matrix_doc['matrix']))
        final_scores += criteria_weights[i] * alt_weights
    
    ranking = [{"name": alt['name'], "score": float(score)} for alt, score in zip(alternatives, final_scores)]
    ranking.sort(key=lambda x: x['score'], reverse=True)
    
    # Vẽ biểu đồ
    names = [item['name'] for item in ranking]
    scores = [item['score'] for item in ranking]
    plt.figure(figsize=(10, 6))
    plt.bar(names, scores, color='skyblue')
    plt.xlabel('Phương án')
    plt.ylabel('Điểm số')
    plt.title('Xếp hạng')
    plt.xticks(rotation=45)
    chart_filename = f"ranking_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    chart_path = os.path.join(CHART_DIR, chart_filename)
    plt.savefig(chart_path, bbox_inches='tight')
    plt.close()
    
    result_doc = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "ranking": ranking,
        "chart": os.path.join('charts', chart_filename).replace('\\', '/')
    }
    results_collection.insert_one(result_doc)
    
    return render_template('result.html', ranking=ranking, chart=result_doc['chart'])

if __name__ == '__main__':
    app.run(debug=True)