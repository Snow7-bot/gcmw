<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, RouterLink } from 'vue-router'

import LiquidBar from '@renderer/components/LiquidBar.vue'
import IntraLinkModal from '@renderer/components/IntraLinkModal.vue'
import LiquidGlass from '@renderer/components/ui/liquid-glass/LiquidGlass.vue'

import { CardData } from '@renderer/components/ui/infinite-grid/types'

type VideoData = CardData & {
  video: string
}

const route = useRoute()
const tag = computed(() => route.params.tag as string | undefined)

const cardData = ref<VideoData[]>([])
const alltag = ref<string[]>([])

const show = computed(() => {
  if (!tag.value) return cardData.value
  return cardData.value.filter((v) => v.tags && v.tags.includes(String(tag.value)))
})

onMounted(async () => {
  const data: VideoData[] = await window.api.getYaml('science-video/index.yml')
  cardData.value = data

  const s = new Set<string>()
  data.forEach((v) => v.tags?.forEach((t) => s.add(t)))
  alltag.value = Array.from(s)
})

const linkModalOpen = ref(false)
const linkModalUrl = ref<string | null>(null)

function openVideo(video: string): void {
  linkModalUrl.value = video
  linkModalOpen.value = true
}
</script>

<template>
  <div class="relative min-h-screen w-full flex flex-col">
    <!-- header -->
    <div class="fixed z-50 w-full mt-[2em] flex flex-col">
      <div class="max-w-5xl mx-auto w-full flex justify-center px-[4em]">
        <LiquidBar :title="'科普影片' + (tag ? ` (${tag})` : '')" back="/" />
      </div>
    </div>

    <div class="w-full mt-[2em] flex flex-col">
      <div class="min-h-16 max-w-5xl mx-auto w-full flex justify-center px-[4em]"></div>

      <!-- tags -->
      <div
        class="mt-[1em] flex flex-nowrap items-center items-center justify-center gap-3 max-w-[80vw] mx-auto pb-[1em] px-[1em]"
      >
        <p class="mr-[0.5em] opacity-[0.8] text-sm shrink-0">筛选:</p>

        <RouterLink v-for="t in alltag" :key="t" :to="`/science/${t}`" class="shrink-0">
          <LiquidGlass :enable-effect="false" container-class="rounded-full">
            <div class="px-4 py-1 text-sm opacity-[0.5] hover:opacity-[0.85]">
              {{ t }}
            </div>
          </LiquidGlass>
        </RouterLink>

        <RouterLink to="/science" class="shrink-0">
          <LiquidGlass :enable-effect="false" container-class="rounded-full">
            <div
              class="flex min-h-8 min-w-8 w-full items-center justify-center opacity-[0.5] hover:opacity-[0.8] text-sm"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24">
                <g
                  fill="none"
                  stroke="currentColor"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  stroke-width="2"
                >
                  <path
                    d="m16 22l-1-4m4-4a1 1 0 0 0 1-1v-1a2 2 0 0 0-2-2h-3a1 1 0 0 1-1-1V4a2 2 0 0 0-4 0v5a1 1 0 0 1-1 1H6a2 2 0 0 0-2 2v1a1 1 0 0 0 1 1"
                  />
                  <path
                    d="M19 14H5l-1.973 6.767A1 1 0 0 0 4 22h16a1 1 0 0 0 .973-1.233zM8 22l1-4"
                  />
                </g>
              </svg>
            </div>
          </LiquidGlass>
        </RouterLink>
      </div>
    </div>

    <!-- content -->
    <div class="pt-[2em] px-[3em] w-full">
      <div class="masonry">
        <div
          v-for="card in show"
          :key="card.video"
          class="masonry-item cursor-pointer"
          @click="openVideo(card.video)"
        >
          <div class="rounded-2xl overflow-hidden bg-white/30 backdrop-blur">
            <img :src="card.image" alt="" class="w-full object-cover" loading="lazy" />

            <div class="p-4 space-y-2">
              <h3 class="text-base font-semibold leading-snug">
                {{ card.title }}
              </h3>

              <p class="text-sm opacity-[0.7] leading-relaxed">
                {{ card.description }}
              </p>

              <div class="flex flex-wrap gap-2 pt-2">
                <span
                  v-for="t in card.tags"
                  :key="t"
                  class="text-xs px-2 py-[2px] rounded-full bg-white/10 opacity-[0.75]"
                >
                  {{ t }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="w-full shrink-0 grow-0 flex items-end justify-center h-[3em]">
        <div
          class="mb-[1em] flex flex-col items-center opacity-[0.5] hover:opacity-[0.8] hover:z-54"
        >
          <p class="w-full align-left select-none text-md">到底了</p>
        </div>
      </div>
    </div>

    <IntraLinkModal v-model:open="linkModalOpen" :url="linkModalUrl" />
  </div>
</template>

<style scoped>
.masonry {
  column-count: 4;
  column-gap: 1.5rem;
}

@media (max-width: 1280px) {
  .masonry {
    column-count: 3;
  }
}

@media (max-width: 900px) {
  .masonry {
    column-count: 2;
  }
}

@media (max-width: 600px) {
  .masonry {
    column-count: 1;
  }
}

.masonry-item {
  break-inside: avoid;
  margin-bottom: 1.5rem;
}
</style>
