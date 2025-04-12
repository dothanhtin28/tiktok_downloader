from flask import Flask, render_template, request, flash, redirect, url_for
import os
import subprocess
import re
import concurrent.futures # Thêm thư viện cho tải song song
from threading import Lock # Để bảo vệ việc ghi vào progress list (nếu cần)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_default_secret_key_for_local_dev')

# --- Cấu hình ---
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(APP_ROOT, "tiktok_cookie.txt")
LOG_FILE = os.path.join(APP_ROOT, "downloaded_videos.txt")
DEFAULT_SAVE_FOLDER = os.path.join(APP_ROOT, "downloads")
CONFIG_FILE = os.path.join(APP_ROOT, "save_path.cfg")

# Khóa để bảo vệ truy cập vào danh sách progress khi dùng đa luồng
progress_lock = Lock()
progress_list = [] # Danh sách tiến trình dùng chung cho các luồng

# --- Hàm hỗ trợ ---

def get_current_cookie():
    """Đọc nội dung cookie từ file."""
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def save_cookie(cookie_content):
    """Lưu nội dung cookie vào file."""
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_content)
        return True
    except IOError as e:
        print(f"Lỗi khi lưu cookie: {e}")
        return False

def get_save_path_setting():
    """Đọc đường dẫn lưu trữ từ file cấu hình."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            path = f.read().strip()
            if os.path.isdir(path): # Kiểm tra thư mục tồn tại trực tiếp
                os.makedirs(path, exist_ok=True) # Đảm bảo tồn tại
                return path
            else:
                print(f"Đường dẫn cấu hình '{path}' không hợp lệ hoặc không tồn tại, sử dụng mặc định.")
                os.makedirs(DEFAULT_SAVE_FOLDER, exist_ok=True)
                # Cập nhật lại file config với đường dẫn mặc định nếu đường dẫn cũ không hợp lệ
                save_save_path_setting(DEFAULT_SAVE_FOLDER)
                return DEFAULT_SAVE_FOLDER
    except FileNotFoundError:
        os.makedirs(DEFAULT_SAVE_FOLDER, exist_ok=True)
        # Tạo file config với đường dẫn mặc định nếu file chưa có
        save_save_path_setting(DEFAULT_SAVE_FOLDER)
        return DEFAULT_SAVE_FOLDER
    except Exception as e:
        print(f"Lỗi khi đọc đường dẫn cấu hình: {e}, sử dụng mặc định.")
        os.makedirs(DEFAULT_SAVE_FOLDER, exist_ok=True)
        return DEFAULT_SAVE_FOLDER


def save_save_path_setting(path):
    """Lưu đường dẫn vào file cấu hình."""
    try:
        # Validate path sơ bộ
        if not path or ".." in path: # Ngăn chặn đường dẫn tương đối nguy hiểm
             flash("Đường dẫn lưu trữ không hợp lệ.", "error")
             return False
        if not os.path.isabs(path):
             # Nếu là đường dẫn tương đối, chuyển thành tuyệt đối dựa trên thư mục gốc
             path = os.path.abspath(os.path.join(APP_ROOT, path))
             print(f"Đã chuyển đường dẫn tương đối thành: {path}")

        # Tạo thư mục nếu chưa tồn tại
        os.makedirs(path, exist_ok=True)

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(path)
        return True
    except Exception as e:
        print(f"Lỗi khi lưu đường dẫn: {e}")
        flash(f"Lỗi khi lưu đường dẫn: {e}", "error")
        return False

def download_video_worker(link, save_path, cookie_header):
    """
    Hàm thực hiện tải video cho một link cụ thể (dùng trong thread).
    Trả về tuple: (link, success_message, error_message, final_filename)
    """
    global progress_list # Sử dụng biến toàn cục

    # Thông báo bắt đầu (cập nhật vào list dùng chung)
    with progress_lock:
        progress_list.append(f"⏳ Bắt đầu tải: {link}")

    if not cookie_header:
        return link, None, "Lỗi: Cookie không được cung cấp cho luồng tải.", None

    # Đảm bảo thư mục lưu tồn tại (an toàn hơn khi gọi lại trong thread)
    try:
        os.makedirs(save_path, exist_ok=True)
    except Exception as e:
        return link, None, f"Lỗi tạo thư mục '{save_path}': {e}", None

    output_template = os.path.join(save_path, "%(title).80s [%(id)s].%(ext)s")
    command = [
        "yt-dlp",
        "--no-warnings", "--no-part", "--progress", # Thêm --progress để yt-dlp hiển thị % (dù chưa bắt được real-time)
        f"--add-header=Cookie: {cookie_header}",
        "-o", output_template,
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        link
    ]

    try:
        # Sử dụng Popen để có thể đọc output nếu muốn nâng cấp sau này
        # Hiện tại vẫn dùng .run() tương đương nhưng nền tảng cho Popen đã có
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore')
        stdout, stderr = process.communicate() # Đợi tiến trình hoàn thành và lấy output
        returncode = process.returncode

        # In log để debug
        # print(f"==== YOUTUBE-DL STDOUT ({link}) ====\n", stdout)
        # print(f"==== YOUTUBE-DL STDERR ({link}) ====\n", stderr)

        output = stdout + stderr
        final_filename = None

        # Trích xuất tên file (giữ nguyên logic cũ)
        match = re.search(r"\[(?:download|Merger)\].*Destination: (.*?)\n", stdout, re.IGNORECASE)
        if not match:
             match = re.search(r"\[(?:Merger|ffmpeg)\].*Merging formats into \"(.*?)\"", stdout, re.IGNORECASE)
        if not match:
             match = re.search(r"\[download\].*?(.*?)\s+has already been downloaded", stdout, re.IGNORECASE)

        if match:
            final_filename = os.path.basename(match.group(1).strip().strip('"'))

        if returncode != 0:
            error_message = f"Lỗi khi tải {link}. Chi tiết: {stderr[-200:]}" # Lấy phần cuối stderr cho gọn
            print(f"Lỗi tải {link}: {output}") # In đầy đủ lỗi ra console server
            return link, None, error_message, None
        else:
            success_message = f"Tải xong: {final_filename if final_filename else link}"
            # Ghi log tên file thực tế nếu có
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{final_filename if final_filename else link}\n")
            except Exception as log_e:
                print(f"Lỗi ghi log cho {link}: {log_e}")
            return link, success_message, None, final_filename

    except FileNotFoundError:
        return link, None, "Lỗi: Không tìm thấy lệnh 'yt-dlp'.", None
    except Exception as e:
        return link, None, f"Lỗi không xác định khi tải {link}: {e}", None

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    global progress_list # Sử dụng biến toàn cục
    current_cookie = get_current_cookie()
    current_save_path = get_save_path_setting()

    if request.method == 'POST':
        action = request.form.get('action')

        # --- Xử lý Cập nhật Cấu hình ---
        if action == 'update_settings':
            new_cookie = request.form.get('cookie', '').strip()
            new_save_path = request.form.get('save_path', '').strip() # Lấy từ input ẩn

            # Cập nhật cookie
            if new_cookie != current_cookie:
                if save_cookie(new_cookie):
                    flash("Đã cập nhật cookie thành công!", "success")
                    current_cookie = new_cookie
                else:
                    flash("Lỗi khi lưu cookie.", "error")

            # Cập nhật đường dẫn
            if new_save_path and new_save_path != current_save_path:
                 if save_save_path_setting(new_save_path):
                      flash(f"Đã cập nhật đường dẫn lưu trữ thành: {new_save_path}", "success")
                      current_save_path = new_save_path
                 # else: flash đã được gọi trong hàm save_save_path_setting

            return redirect(url_for('index'))

        # --- Xử lý Tải Video ---
        elif action == 'download':
            links_raw = request.form.get('links', '')
            links = [l.strip() for l in links_raw.strip().splitlines() if l.strip()]

            if not links:
                flash("Vui lòng nhập ít nhất một link video.", "warning")
                return render_template("index.html", progress=[], current_cookie=current_cookie, current_save_path=current_save_path)

            if not current_cookie:
                 flash("Cookie chưa được thiết lập. Vui lòng nhập cookie và lưu lại.", "error")
                 return render_template("index.html", progress=[], current_cookie=current_cookie, current_save_path=current_save_path)

            download_save_path = get_save_path_setting()
            progress_list = [] # Xóa list cũ trước khi bắt đầu lượt tải mới
            num_workers = min(len(links), 8) # Giới hạn số luồng tối đa (ví dụ 8)

            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                # Gửi các tác vụ tải vào executor
                future_to_link = {executor.submit(download_video_worker, link, download_save_path, current_cookie): link for link in links}

                # Xử lý kết quả khi từng luồng hoàn thành
                for future in concurrent.futures.as_completed(future_to_link):
                    link_done = future_to_link[future]
                    try:
                        _link, success_msg, error_msg, _filename = future.result()
                        with progress_lock: # Khóa khi cập nhật list dùng chung
                            if error_msg:
                                progress_list.append(f"❌ {error_msg}")
                            elif success_msg:
                                progress_list.append(f"✅ {success_msg}")
                            else: # Trường hợp không mong muốn
                                progress_list.append(f"❓ Kết quả không xác định cho: {link_done}")
                    except Exception as exc:
                        print(f'Link {link_done} tạo ra exception: {exc}')
                        with progress_lock:
                            progress_list.append(f"💥 Lỗi nghiêm trọng khi xử lý link: {link_done} - {exc}")

            flash(f"Đã hoàn tất xử lý {len(links)} link (kiểm tra kết quả bên dưới).", "info")
            # Render lại trang với danh sách progress đã được cập nhật bởi các luồng
            return render_template("index.html",
                                   progress=progress_list, # Truyền list đã được cập nhật
                                   current_cookie=current_cookie,
                                   current_save_path=current_save_path)

    # --- Xử lý GET Request ---
    # Reset progress_list khi tải lại trang bằng GET
    progress_list = []
    return render_template("index.html",
                           progress=None, # Không hiển thị log cũ khi mới vào trang
                           current_cookie=current_cookie,
                           current_save_path=current_save_path)

if __name__ == '__main__':
    # Tạo file/thư mục cần thiết nếu chưa có
    if not os.path.exists(COOKIES_FILE): open(COOKIES_FILE, 'a').close()
    if not os.path.exists(LOG_FILE): open(LOG_FILE, 'a').close()
    if not os.path.exists(CONFIG_FILE): save_save_path_setting(DEFAULT_SAVE_FOLDER) # Tạo config nếu chưa có
    os.makedirs(get_save_path_setting(), exist_ok=True) # Đảm bảo thư mục lưu tồn tại khi khởi động

    app.run(debug=True, threaded=True) # Chạy Flask với threaded=True để hỗ trợ tốt hơn cho concurrent.futures
