<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

const el = ref<HTMLSpanElement | null>(null)

let rafId = 0

const update = (): void => {
  const doc = document.documentElement
  const scrollTop = doc.scrollTop
  const max = doc.scrollHeight - doc.clientHeight

  const progress = max > 0 ? scrollTop / max : 0
  const percent = Math.round(progress * 100)

  if (el.value) {
    if (percent > 0.1) el.value.textContent = `${percent}%`
    else el.value.textContent = ``
  }
}

const onScroll = (): void => {
  // 用 rAF，避免 scroll 事件过频
  if (rafId) return
  rafId = requestAnimationFrame(() => {
    update()
    rafId = 0
  })
}

onMounted(() => {
  update()
  window.addEventListener('scroll', onScroll, { passive: true })
})

onUnmounted(() => {
  window.removeEventListener('scroll', onScroll)
  if (rafId) cancelAnimationFrame(rafId)
})
</script>

<template>
  <span ref="el"></span>
</template>
