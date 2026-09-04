<script setup lang="ts">
// import { InteractiveGridPattern } from '@renderer/components/ui/interactive-grid-pattern'
import { RouterLink } from 'vue-router'
import { LiquidGlass } from '@renderer/components/ui/liquid-glass'

const navList = [
  { path: '/qa', label: '互动问答' },
  { path: '/science', label: '视光科普' },
  { path: '/about', label: '特色技术' },
  { path: '/member', label: '人员介绍' }
]
</script>

<template>
  <div class="index-root relative grid h-[100vh] w-full overflow-hidden">
    <div class="absolute flex h-full w-full flex-col justify-center items-center overflow-hidden">
      <div class="grow-3"></div>
      <!-- <img :src="'rc://favico.png'" class="block h-[3rem] w-[3rem] mb-[2rem]" /> -->
      <p
        class="z-10 text-center xl:text-8xl text-6xl mb-[0.5em] font-semibold tracking-[0.25em] whitespace-pre-wrap text-[#168378] dark:text-white"
      >
        天津市眼科医院
      </p>
      <p
        class="z-10 text-center xl:text-8xl text-6xl mb-[3em] font-semibold tracking-[0.25em] whitespace-pre-wrap text-[#168378] dark:text-white"
      >
        视光中心
      </p>
      <div class="z-10 flex flex-wrap items-center justify-center gap-8 z-51 max-w-[80vw] mx-auto">
        <RouterLink v-for="nav in navList" :key="nav.path" :to="nav.path">
          <LiquidGlass container-class="rounded-full" :blur="6" :scale="120">
            <div class="flex min-h-12 w-full px-8 items-center justify-center">
              {{ nav.label }}
            </div>
          </LiquidGlass>
        </RouterLink>
      </div>
      <div class="grow-3"></div>
    </div>
    <!-- <div class="absolute grid h-[300%] top-[-100%] w-full place-content-center overflow-hidden">
      <InteractiveGridPattern
        :width="48"
        :height="48"
        :squares="[63, 47]"
        :pin="[
          1417 - 63 - 1,
          1417 - 63 * 2 - 1,
          1417 - 63 * 3 - 1,
          1417 - 63 * 4 - 1,
          1417 - 63 * 5 - 1,
          1417 - 63 * 5 - 2,
          1417 - 63 * 5 - 3,
          1417 - 63 * 5 - 4,
          1417 - 63 * 5 - 5,
          1417 - 63 - 5,
          1417 - 63 * 2 - 5,
          1417 - 63 * 3 - 5,
          1417 - 63 * 4 - 5,
          1417 - 63 * 5 - 5,
          1417 - 63 - 3,
          1417 - 63 * 2 - 3,
          1417 - 63 * 3 - 3,
          1417 - 63 * 4 - 3,
          1417 - 63 * 5 - 3,
          1417 - 63 + 1,
          1417 - 63 + 2,
          1417 - 63 + 3,
          1417 - 63 + 4,
          1417 - 63 + 5,
          1417 - 63 * 3 + 1,
          1417 - 63 * 3 + 2,
          1417 - 63 * 3 + 3,
          1417 - 63 * 3 + 4,
          1417 - 63 * 3 + 5,
          1417 - 63 * 5 + 1,
          1417 - 63 * 5 + 2,
          1417 - 63 * 5 + 3,
          1417 - 63 * 5 + 4,
          1417 - 63 * 5 + 5,
          1417 - 63 * 4 + 5,
          1417 - 63 * 2 + 5,
          1417 + 63 - 1,
          1417 + 63 - 2,
          1417 + 63 - 3,
          1417 + 63 - 4,
          1417 + 63 - 5,
          1417 + 63 * 3 - 1,
          1417 + 63 * 3 - 2,
          1417 + 63 * 3 - 3,
          1417 + 63 * 3 - 4,
          1417 + 63 * 3 - 5,
          1417 + 63 * 5 - 1,
          1417 + 63 * 5 - 2,
          1417 + 63 * 5 - 3,
          1417 + 63 * 5 - 4,
          1417 + 63 * 5 - 5,
          1417 + 63 * 2 - 5,
          1417 + 63 * 4 - 5,
          1417 + 63 * 1 + 1,
          1417 + 63 * 2 + 1,
          1417 + 63 * 3 + 1,
          1417 + 63 * 4 + 1,
          1417 + 63 * 5 + 1,
          1417 + 63 * 1 + 3,
          1417 + 63 * 2 + 3,
          1417 + 63 * 3 + 3,
          1417 + 63 * 4 + 3,
          1417 + 63 * 5 + 3,
          1417 + 63 * 1 + 5,
          1417 + 63 * 2 + 5,
          1417 + 63 * 3 + 5,
          1417 + 63 * 4 + 5,
          1417 + 63 * 5 + 5,
          1417 + 63 * 5 + 2,
          1417 + 63 * 5 + 4
        ]"
        class="skew-y-12 [mask-image:radial-gradient(120vh_circle_at_center,white,transparent)]"
      />
    </div> -->
  </div>
</template>

<style scoped>
.index-root {
  background: url(rc://bg.png) center / cover no-repeat fixed;
}
</style>
