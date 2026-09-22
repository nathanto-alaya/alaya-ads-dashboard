# Alaya Ads Dashboard — Bộ file MỚI NHẤT (22 Sep 2026)

Đây là bộ file đầy đủ nhất. Dùng bộ này làm chuẩn, up lên GitHub rồi làm tiếp từ đây.

## 3 file trong bộ này

### 1. index.html  ⬅️ CẦN DEPLOY (bản này mới hơn GitHub)
Bản trên GitHub hiện tại THIẾU 2 thứ cuối. File này có ĐẦY ĐỦ:
- ✅ Dark mode (mặc định tối, nút toggle ☾/☀ góc phải, nhớ lựa chọn)
- ✅ Accent xanh sáng #5b8def (Royal Blue làm sáng cho nền tối)
- ✅ Funnel review pick được ngày (tab Funnel → Custom → 2 ô date)
- ✅ Date picker kiểu Meta cho các tab Meta (preset đầy đủ + lịch 2 tháng chọn range)
- ✅ Ghi chú ngày cụ thể dưới date picker (bấm Today → hiện "22 Sep 2026")  ← MỚI, chưa lên GitHub
- ✅ Compare mode (tick "Compare to previous period" → mỗi KPI hiện % thay đổi + "Prev: ...")  ← MỚI, chưa lên GitHub

→ Việc cần làm: thay index.html trên GitHub bằng file này, commit main.

### 2. update_funnel.py  ✅ ĐÃ TRÙNG GITHUB (không cần deploy lại)
Giống hệt bản đang chạy trên GitHub. Đã gồm mọi fix:
- Fix location trống (dùng default khi secret rỗng)
- Fix search endpoint (bỏ locationId, bỏ date param, bỏ sort — kéo hết rồi lọc trong code)
- Fix weekly.json (log không còn KeyError)
- Lọc lead test 2 lớp: email nội bộ (@alayaproperty.com, @socialwave.com.au) + tên chứa "test/dummy/demo"

→ Không cần làm gì. Để đây để đối chiếu.

### 3. update_dashboard.py  ✅ LẤY TỪ GITHUB (đã là bản mới nhất)
Bản này copy thẳng từ GitHub vì GitHub đang giữ bản mới nhất (có chunking + rate limit fix).
- Chunk ad-level insights theo cửa sổ 30 ngày (tránh lỗi "reduce the amount of data")
- Xử lý rate limit Meta (back off 1/5/15/30 phút)

→ Không cần làm gì. Để đây để đối chiếu.

## Tóm lại: chỉ cần deploy index.html

Hai file .py đã khớp GitHub rồi. Chỉ có index.html là mới hơn (thêm ghi chú ngày + compare).

**Deploy:**
1. Vào github.com/nathanto-alaya/alaya-ads-dashboard
2. Mở index.html → sửa → xóa hết → dán nội dung index.html trong bộ này → commit main
3. Cloudflare tự deploy, hard refresh (Cmd+Shift+R)

## Trạng thái tính năng dashboard (để khỏi lạc)

| Tính năng | Trạng thái |
|---|---|
| 4 tab: Campaign / Creative / Ad set / Funnel | ✅ chạy |
| Lead thật từ GHL (join UTM) | ✅ chạy |
| Lọc lead test | ✅ chạy |
| Cost per lead (GHL + Meta) | ✅ chạy |
| Ghi chú "Meta gồm cả test" | ✅ chạy |
| Refresh tự động 2 lần/ngày (8am + 4pm AEST) | ✅ chạy |
| Cloudflare Access (chặn theo email) | ✅ chạy |
| Dark mode | ✅ trong index.html mới |
| Date picker kiểu Meta + ghi chú ngày | ✅ trong index.html mới |
| Compare mode (KPI cards tab Campaign) | ✅ trong index.html mới |

## Việc có thể làm tiếp (chưa làm)

- Compare mở rộng vào bảng campaign/creative (mỗi dòng có cột kỳ trước) — hiện compare chỉ ở 6 KPI card tab Campaign
- Redesign Modern SaaS sâu hơn cho từng tab (mới đổi theme nền, chưa đổi layout)
- Speed-to-lead metric (đo thời gian lead vào → gọi đầu) — từng bàn khi phân tích booking rate
