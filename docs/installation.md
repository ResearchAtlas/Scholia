# Installing Scholia

[English](#english) · [中文](#中文)

## English

Scholia runs on Macs with Apple silicon (M1 or later) and macOS 14 Sonoma or later. It is in early development and not ready to use yet.

### Install

1. Open the `Scholia.dmg` file.
2. In the window that opens, drag **Scholia** onto the **Applications** folder.
3. Eject the disk image.

### Open it the first time

Scholia is not yet signed with an Apple Developer ID or notarized by Apple, so macOS blocks it the first time you open it.

1. Open Scholia from the Applications folder. macOS says it cannot be opened. Click **Done**.
2. Open **System Settings**, then **Privacy & Security**.
3. Scroll down to the message about Scholia and click **Open Anyway**.
4. When the warning appears again, click **Open**.

After that, Scholia opens normally. Apple describes these steps in [Safely open apps on your Mac](https://support.apple.com/en-us/102445).

### Updates

Install a new version the same way, replacing the old one in Applications. Each new version is expected to need **Open Anyway** once more.

### Uninstall

Quit Scholia and drag it from the Applications folder to the Trash. The app keeps its data, including your projects and its backups, in the folder `~/Library/Application Support/Scholia`. To remove that data as well, delete that folder. Provider keys are kept in the macOS Keychain under `io.github.researchatlas.scholia`; remove them in the Keychain Access app.

## 中文

Scholia 适用于搭载 Apple 芯片（M1 或更新）、运行 macOS 14 Sonoma 或更新版本的 Mac。它仍处于早期开发阶段，暂时还不能使用。

### 安装

1. 打开 `Scholia.dmg` 文件。
2. 在弹出的窗口中，把 **Scholia** 拖到 **Applications**（应用程序）文件夹。
3. 推出磁盘映像。

### 第一次打开

Scholia 目前还没有使用 Apple Developer ID 签名，也没有经过 Apple 公证，所以第一次打开时 macOS 会阻止它。

1. 从“应用程序”文件夹打开 Scholia。macOS 会提示无法打开，点按 **完成**。
2. 打开 **系统设置**，然后进入 **隐私与安全性**。
3. 向下滚动到关于 Scholia 的提示，点按 **仍要打开**。
4. 再次出现警告时，点按 **打开**。

之后 Scholia 就可以正常打开。Apple 在[在 Mac 上安全打开 App](https://support.apple.com/zh-cn/102445)中介绍了这些步骤。

### 更新

用同样的方法安装新版本，替换“应用程序”中的旧版本。每个新版本预计都需要再点按一次 **仍要打开**。

### 卸载

退出 Scholia，然后把它从“应用程序”文件夹拖到废纸篓。App 把数据（包括你的项目和备份）保存在 `~/Library/Application Support/Scholia` 文件夹中。如果也要删除这些数据，请删除这个文件夹。服务商密钥保存在 macOS 钥匙串中，名称为 `io.github.researchatlas.scholia`，可以在“钥匙串访问”App 中删除。
