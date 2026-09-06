<script lang="ts" setup>
import { ref, watch, nextTick, onMounted, onUnmounted } from 'vue'

interface Props {
  path: string
}

const props = defineProps<Props>()
const content = ref<string | null>(null)

async function typesetMath(): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if ((window as any).MathJax?.typesetPromise) {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      await (window as any).MathJax.typesetPromise()
    } catch (e) {
      console.error('MathJax typeset failed:', e)
    }
  }
}

watch(
  () => props.path,
  async (path) => {
    content.value = null

    if (!(await window.api.hasMarkdown(path))) {
      return
    }

    try {
      const data = await window.api.getMarkdown(path)
      content.value = data

      await nextTick()
      await typesetMath()
    } catch (e) {
      console.error('getMarkdown failed:', e)
    }
  },
  { immediate: true }
)

onMounted(async () => {
  await nextTick()
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const MJ = (window as any).MathJax
  if (MJ?.typesetPromise) {
    await MJ.typesetPromise()
  }
})

const markdownRef = ref<HTMLElement | null>(null)
const anchorIndex = new Map<string, HTMLElement>()

function findScrollParent(el: HTMLElement): HTMLElement | null {
  let cur: HTMLElement | null = el.parentElement

  while (cur) {
    const style = window.getComputedStyle(cur)
    if (/(auto|scroll)/.test(style.overflowY)) {
      return cur
    }
    cur = cur.parentElement
  }

  return document.scrollingElement as HTMLElement | null
}

function highlightAnchor(target: HTMLElement): void {
  const CLASS = 'md-anchor-highlight'

  target.classList.remove(CLASS)
  // 强制 reflow，保证动画能重新触发
  void target.offsetWidth
  target.classList.add(CLASS)

  window.setTimeout(() => {
    target.classList.remove(CLASS)
  }, 3000)
}

function scrollToAnchor(target: HTMLElement): void {
  const container = markdownRef.value
  if (!container) return

  const scrollParent = findScrollParent(container)

  // document 滚动，直接交给浏览器
  if (
    !scrollParent ||
    scrollParent === document.body ||
    scrollParent === document.documentElement
  ) {
    const vh = window.innerHeight
    const offset = vh / 3

    const y = window.scrollY + target.getBoundingClientRect().top - offset

    window.scrollTo({
      top: y,
      behavior: 'smooth'
    })
    return
  }

  let top = target.offsetTop
  let el = target.offsetParent as HTMLElement | null

  while (el && el !== scrollParent) {
    top += el.offsetTop
    el = el.offsetParent as HTMLElement | null
  }

  const offset = scrollParent.clientHeight / 3

  scrollParent.scrollTo({
    top: Math.max(0, top - offset),
    behavior: 'smooth'
  })
}

import IntraLinkModal from '@renderer/components/IntraLinkModal.vue'
const linkModalOpen = ref(false)
const linkModalUrl = ref<string | null>(null)

function isSafeExternalUrl(url: string): boolean {
  const trimmed = url.trim().toLowerCase()
  return trimmed.startsWith('rc:') || /^(https?|mailto):/i.test(trimmed)
}

function handleMarkdownLink(href: string): void {
  if (!href.startsWith('#')) {
    if (!isSafeExternalUrl(href)) {
      console.warn('Blocked unsafe link:', href)
      return
    }
    linkModalUrl.value = href
    linkModalOpen.value = true
    return
  }

  const id = decodeURIComponent(href.slice(1))
  if (!id) return

  const target = anchorIndex.get(id)
  if (!target) {
    console.warn('anchor not found:', id)
    return
  }

  scrollToAnchor(target)
  highlightAnchor(target)
}

function handleLinkClick(e: MouseEvent): void {
  const target = e.target as HTMLElement
  const anchor = target.closest('a')

  if (!anchor) return

  const href = anchor.getAttribute('href')
  if (!href) return

  e.preventDefault()
  e.stopPropagation()

  handleMarkdownLink(href)
}

function findHeadingByText(root: HTMLElement, text: string): HTMLElement | null {
  const headings = root.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6')

  for (const h of headings) {
    if (h.textContent?.trim() === text) {
      return h
    }
  }

  return null
}

function buildAnchorIndex(root: HTMLElement): void {
  anchorIndex.clear()

  const withId = root.querySelectorAll<HTMLElement>('[id]')
  for (const el of withId) {
    anchorIndex.set(el.id, el)
  }

  const tocLinks = root.querySelectorAll<HTMLAnchorElement>('.table-of-contents a[href^="#"]')

  for (const a of tocLinks) {
    const raw = a.getAttribute('href')
    if (!raw) continue

    const key = decodeURIComponent(raw.slice(1))
    const title = a.textContent?.trim()
    if (!title) continue

    // 找最匹配的标题（按文本）
    const heading = findHeadingByText(root, title)
    if (!heading) continue

    anchorIndex.set(key, heading)
  }
}

watch(content, async (val) => {
  if (!val) return

  await nextTick()

  const el = markdownRef.value
  if (!el) return

  buildAnchorIndex(el)

  el.removeEventListener('click', handleLinkClick)
  el.addEventListener('click', handleLinkClick)
})

onUnmounted(() => {
  markdownRef.value?.removeEventListener('click', handleLinkClick)
})
</script>

<template>
  <!-- v-html is allowed only after markdown-it html:false + validateLink allowlist in api.ts -->
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div v-if="content" ref="markdownRef" class="markdown-body w-full" v-html="content"></div>
  <IntraLinkModal v-model:open="linkModalOpen" :url="linkModalUrl" />
</template>

<style>
.md-anchor-highlight {
  position: relative;
  animation: md-anchor-flash 3s ease-out;
}

@keyframes md-anchor-flash {
  0% {
    background-color: rgba(255, 230, 150, 0.8);
  }
  100% {
    background-color: transparent;
  }
}
</style>
