import path from 'path'
import fs from 'node:fs'
import YAML from 'yaml'

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

export const hasYaml = (_e: IpcMainInvokeEvent, ymlPath: string): boolean => {
  const fullPath = path.isAbsolute(ymlPath) ? ymlPath : path.join(resourceDir, ymlPath)

  if (!fs.existsSync(fullPath)) {
    return false
  }

  if (!fullPath.endsWith('.yml')) {
    return false
  }

  const resolved = path.resolve(resourceDir, ymlPath)
  if (!resolved.startsWith(path.resolve(resourceDir))) {
    return false
  }

  return true
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const getYaml = async (_e: IpcMainInvokeEvent, ymlPath: string): Promise<any> => {
  const fullPath = path.isAbsolute(ymlPath) ? ymlPath : path.join(resourceDir, ymlPath)

  if (!fs.existsSync(fullPath)) {
    throw new Error(`Yaml file not found: ${fullPath}`)
  }

  if (!fullPath.endsWith('.yml')) {
    throw new Error('Only yaml files are allowed')
  }

  const resolved = path.resolve(resourceDir, ymlPath)
  if (!resolved.startsWith(path.resolve(resourceDir))) {
    throw new Error('Invalid path')
  }

  const raw = fs.readFileSync(fullPath, 'utf-8')
  const parsed = YAML.parse(raw)

  return parsed
}

import MarkdownIt from 'markdown-it'
// import mathjax from 'markdown-it-mathjax3'
import footnote from 'markdown-it-footnote'
import toc from 'markdown-it-table-of-contents'
import mermaid from 'markdown-it-mermaid'
import { IpcMainInvokeEvent } from 'electron'

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true
})

// md.use(mathjax)

md.use(footnote)

md.use(toc, {
  markerPattern: /^\[?\[toc\]\]?/im,
  includeLevel: [1, 2, 3, 4, 5],
  containerClass: 'table-of-contents'
})

// eslint-disable-next-line @typescript-eslint/no-explicit-any
md.use((mermaid as any).default ?? mermaid)

export const hasMarkdown = (_e: IpcMainInvokeEvent, mdPath: string): boolean => {
  const fullPath = path.isAbsolute(mdPath) ? mdPath : path.join(resourceDir, mdPath)

  if (!fs.existsSync(fullPath)) {
    return false
  }

  if (!fullPath.endsWith('.md')) {
    return false
  }

  const resolved = path.resolve(resourceDir, mdPath)
  if (!resolved.startsWith(path.resolve(resourceDir))) {
    return false
  }

  return true
}

import crypto from 'node:crypto'

function getCacheDir(): string {
  return path.join(resourceDir, '.md-cache')
}

function ensureCacheDir(): void {
  const dir = getCacheDir()
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true })
  }
}

function getCacheFilePath(mdFullPath: string): string {
  const hash = crypto.createHash('sha1').update(mdFullPath).digest('hex')

  return path.join(getCacheDir(), `${hash}.json`)
}

interface MarkdownFileCache {
  mtimeMs: number
  html: string
}
export const getMarkdown = async (_e: IpcMainInvokeEvent, mdPath: string): Promise<string> => {
  const fullPath = path.isAbsolute(mdPath) ? mdPath : path.join(resourceDir, mdPath)

  if (!fs.existsSync(fullPath)) {
    throw new Error(`Markdown file not found: ${fullPath}`)
  }

  if (!fullPath.endsWith('.md')) {
    throw new Error('Only markdown files are allowed')
  }

  const resolved = path.resolve(fullPath)
  const base = path.resolve(resourceDir)
  if (!resolved.startsWith(base)) {
    throw new Error('Invalid path')
  }

  ensureCacheDir()

  const stat = fs.statSync(resolved)
  const mtimeMs = stat.mtimeMs
  const cacheFile = getCacheFilePath(resolved)

  // —— 1. 尝试命中缓存 ——
  if (fs.existsSync(cacheFile)) {
    try {
      const cached: MarkdownFileCache = JSON.parse(fs.readFileSync(cacheFile, 'utf-8'))

      if (cached.mtimeMs === mtimeMs) {
        return cached.html
      }
    } catch {
      // 缓存损坏，忽略，走重建
    }
  }

  // —— 2. 重新渲染 ——
  const raw = fs.readFileSync(resolved, 'utf-8')
  const html = md.render(raw)

  // —— 3. 写入缓存（原子替换，避免半写） ——
  const tmp = `${cacheFile}.tmp`
  const cacheData: MarkdownFileCache = {
    mtimeMs,
    html
  }

  fs.writeFileSync(tmp, JSON.stringify(cacheData), 'utf-8')
  fs.renameSync(tmp, cacheFile)

  return html
}
