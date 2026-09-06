import { app, shell, BrowserWindow, ipcMain } from 'electron'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import { join } from 'node:path'
import { getMarkdown, getYaml, hasMarkdown, hasYaml } from './api'
import { protocol } from 'electron'
import path from 'node:path'
import fs from 'node:fs'
import { Readable } from 'node:stream'
import mime from 'mime-types'

let exitArmed = false
let exitInputBuffer = ''
let exitArmedAt = 0
const EXIT_PASSWORD = process.env.GCMW_EXIT_PASSWORD || ''

function createWindow(): void {
  // Create the browser window.
  const mainWindow = new BrowserWindow({
    ...(!process.env.DEBUG
      ? {
          fullscreen: true,
          frame: false,
          kiosk: true,
          skipTaskbar: true
        }
      : {}),
    show: true,
    autoHideMenuBar: true,
    // ...(process.platform === 'linux' ? { icon } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      devTools: !app.isPackaged
    }
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow.setAlwaysOnTop(true, 'screen-saver')
    mainWindow.show()
    mainWindow.focus()
  })

  if (!process.env.DEBUG) {
    mainWindow.on('blur', () => {
      mainWindow.focus()
    })

    mainWindow.on('close', (e) => {
      e.preventDefault()
    })
  }

  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.type !== 'keyDown') return
    if (input.isAutoRepeat || input.alt || input.control || input.meta || input.shift) {
      return
    }
    if (input.location !== 0) return

    if (input.key === 'Escape') {
      exitArmed = !exitArmed
      exitInputBuffer = ''
      exitArmedAt = Date.now()
      return
    }

    if (!exitArmed) return

    if (Date.now() - exitArmedAt > 1000) {
      exitArmed = false
      exitInputBuffer = ''
      return
    }

    if (input.key.length !== 1) {
      exitArmed = false
      exitInputBuffer = ''
      return
    }

    exitInputBuffer += input.key
    exitArmedAt = Date.now()

    if (!EXIT_PASSWORD.startsWith(exitInputBuffer)) {
      exitArmed = false
      exitInputBuffer = ''
      return
    }

    if (exitInputBuffer === EXIT_PASSWORD) {
      app.exit(0)
    }
  })

  mainWindow.webContents.session.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders
      }
    })
  })

  app.on('render-process-gone', () => {
    app.relaunch()
    app.exit()
  })

  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  // HMR for renderer base on electron-vite cli.
  // Load the remote URL for development or the local html file for production.
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

function getResourceDir(): string {
  if (!process.env.RCPATH) return process.cwd()
  let rcPath = process.env.RCPATH
  // 在 Windows 上，将 Unix 风格绝对路径（如 /d/config）转换为 Windows 路径（D:/config）
  // bash (Git Bash/MSYS2) 的 /d/config 在 Node.js 的 path.resolve() 中不会被正确解析
  if (process.platform === 'win32' && /^\/[a-zA-Z]\//.test(rcPath)) {
    rcPath = rcPath[1] + ':' + rcPath.substring(2)
  }
  return path.resolve(rcPath)
}

const resourceDir = getResourceDir()

function isRealPathInside(base: string, target: string): boolean {
  let realBase: string
  let realTarget: string
  try {
    realBase = fs.realpathSync(base)
    realTarget = fs.realpathSync(target)
  } catch {
    return false
  }
  const rel = path.relative(realBase, realTarget)
  return rel === '' || (!rel.startsWith(`..${path.sep}`) && rel !== '..' && !path.isAbsolute(rel))
}

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'rc',
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      stream: true
    }
  }
])

// This method will be called when Electron has finished
// initialization and is ready to create browser windows.
// Some APIs can only be used after this event occurs.
app.whenReady().then(() => {
  // Set app user model id for windows
  electronApp.setAppUserModelId('com.electron')

  // Default open or close DevTools by F12 in development
  // and ignore CommandOrControl + R in production.
  // see https://github.com/alex8088/electron-toolkit/tree/master/packages/utils
  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  protocol.handle('rc', async (request) => {
    try {
      const rawPath = request.url.replace(/^rc:\/\//, '')
      const urlPath = decodeURIComponent(rawPath)
      const filePath = path.resolve(path.join(resourceDir, urlPath))

      if (!fs.existsSync(filePath)) {
        console.error(`[rc://] File not found: ${filePath} (requested: ${request.url})`)
        return new Response(null, { status: 404 })
      }

      // 防目录穿越，使用 realpath 防止符号链接逃逸
      if (!isRealPathInside(resourceDir, filePath)) {
        return new Response(null, { status: 403 })
      }

      const stat = fs.statSync(filePath)
      const size = stat.size
      const range = request.headers.get('range')

      const mimeType = mime.lookup(filePath) || 'application/octet-stream'

      // ===== Range 请求（视频播放关键）=====
      if (range) {
        const match = /bytes=(\d+)-(\d*)/.exec(range)
        if (!match) {
          return new Response(null, { status: 416 })
        }

        const start = Number(match[1])
        const end = match[2] ? Number(match[2]) : size - 1

        if (start >= size || start > end) {
          return new Response(null, { status: 416 })
        }

        const safeEnd = Math.min(end, size - 1)

        const nodeStream = fs.createReadStream(filePath, { start, end: safeEnd })
        const webStream = Readable.toWeb(nodeStream)

        return new Response(webStream as BodyInit, {
          status: 206,
          headers: {
            'Content-Type': mimeType,
            'Accept-Ranges': 'bytes',
            'Content-Range': `bytes ${start}-${end}/${size}`,
            'Content-Length': String(end - start + 1)
          }
        })
      }

      // ===== 普通请求 =====
      const nodeStream = fs.createReadStream(filePath)
      const webStream = Readable.toWeb(nodeStream)

      return new Response(webStream as BodyInit, {
        headers: {
          'Content-Type': mimeType,
          'Accept-Ranges': 'bytes',
          'Content-Length': String(size)
        }
      })
    } catch {
      return new Response(null, { status: 500 })
    }
  })

  ipcMain.handle('getyml', getYaml)
  ipcMain.handle('getmd', getMarkdown)
  ipcMain.handle('hasyml', hasYaml)
  ipcMain.handle('hasmd', hasMarkdown)

  createWindow()

  app.on('activate', function () {
    // On macOS it's common to re-create a window in the app when the
    // dock icon is clicked and there are no other windows open.
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

// Quit when all windows are closed, except on macOS. There, it's common
// for applications and their menu bar to stay active until the user quits
// explicitly with Cmd + Q.
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

// In this file you can include the rest of your app's specific main process
// code. You can also put them in separate files and require them here.
