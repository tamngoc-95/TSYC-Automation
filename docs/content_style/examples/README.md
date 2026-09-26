# Examples — TSYC Content Style References

Thư mục này lưu các mô tả sản phẩm **đã được chủ tiệm chấp nhận** (approved), dùng làm **tham khảo về văn phong**, không phải nguồn sự thật (facts) cho các cuốn sách khác.

## Nguyên tắc

- Mỗi file là **ví dụ về cách viết** (giọng điệu, cấu trúc, cách xử lý ngôn ngữ) — KHÔNG phải kho dữ liệu để copy nội dung/sự kiện sang sách khác.
- Thông tin cụ thể (tên tác giả, cốt truyện, chi tiết) **và tên sách dịch EN/DE** của một ví dụ **không bao giờ** được dùng làm dữ liệu sự thật cho một sản phẩm khác, kể cả khi hai sách có chủ đề giống nhau. Tên dịch trong ví dụ chỉ minh họa cách localization, không phải tên chính thức đã xác minh.
- Chỉ thêm ví dụ mới khi có **xác nhận rõ ràng** rằng chủ tiệm đã chấp nhận bản mô tả đó là bản cuối (không chỉ vì Claude tạo ra nó).
- Bản nháp chưa được duyệt **phải nằm ngoài** bộ ví dụ chuẩn này (không lưu vào thư mục `examples/`).

## Định dạng tên file

```
<book-slug>.md
```

Ví dụ: `khong-gia-dinh.md`, `dung-dinh-trang-di.md`

## Cấu trúc mỗi file ví dụ

```markdown
# Book / Product

## Why this example is retained

## Vietnamese

## English

## German

## Style notes

## Source note
```

Nếu một ngôn ngữ không có trong nguồn hội thoại gốc, ghi rõ:

```
NOT_AVAILABLE_IN_SOURCE_CHAT
```

không được tự dịch/tự viết thêm để lấp chỗ trống.

## Trạng thái hiện tại (từ phiên chat ngày tạo guide này)

Trong phiên hội thoại nguồn (giới thiệu ~25 sách), **chỉ có 1 trường hợp** có tín hiệu chấp nhận rõ ràng đủ để lưu làm ví dụ chuẩn: **"Đủng đỉnh Trăng đi"** — chủ tiệm yêu cầu viết lại theo hướng "trau chuốt, ngọt ngào hơn, 249 từ" rồi ngay sau đó yêu cầu dịch bản đó sang Anh/Đức (không sửa thêm) → được xem là bản cuối đã chấp nhận. Xem `dung-dinh-trang-di.md`.

Tất cả các mô tả sách khác trong phiên đó **không có phản hồi tường minh kiểu "được/duyệt/tốt"** — người dùng chỉ tiếp tục gửi ảnh sách mới. Theo đúng quy tắc "không giả định một bản là approved chỉ vì Claude tạo ra nó", các mô tả đó **không** được đưa vào đây, mà được liệt kê trong báo cáo là `POTENTIAL_EXAMPLE_REQUIRES_REVIEW` — chủ tiệm có thể tự chọn cuốn nào muốn chốt làm ví dụ chuẩn sau này.
