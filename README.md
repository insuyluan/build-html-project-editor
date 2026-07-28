# HTML Project Editor — GitHub Actions Builder

Bộ này tạo static ZIP chuẩn hóa cho HTML Project Editor bằng GitHub Actions.

## Thiết lập một lần

1. Tạo một repository GitHub **Private** trống.
2. Chép toàn bộ nội dung bộ này vào repository và push lên nhánh mặc định.
3. Trong Settings → Actions → General, cho phép Actions chạy.
4. Tạo Fine-grained personal access token chỉ dành cho repository này:
   - Actions: Read and write
   - Contents: Read and write
5. Trong Source Build Workspace, chọn GitHub Actions rồi nhập:
   - Repository: owner/repository
   - Branch: tên nhánh chứa workflow
   - Token: token vừa tạo

## Cách hoạt động

Editor tạo một draft release tạm, tải source.zip lên, kích hoạt workflow,
nhận result.zip rồi xóa release và tag tạm. Token chỉ được lưu trong phiên tab.

## Giới hạn an toàn

- Source ZIP: tối đa 30 MB
- Thời gian mỗi build: tối đa 15 phút
- Output: tối đa 120 MB
- Không đưa token vào repository hoặc tệp ZIP
