<script lang="ts" setup>
import { AnimatedTestimonials } from '@renderer/components/ui/animated-testimonials'
import LiquidBar from '@renderer/components/LiquidBar.vue'
import { useRoute } from 'vue-router'
import { computed, onMounted, ref } from 'vue'
import MarkdownContent from '@renderer/components/MarkdownContent.vue'
import { LiquidGlass } from '@renderer/components/ui/liquid-glass'

const route = useRoute()
const idx = computed(() => Number(route.params.id))
const tag = computed(() => route.params.tag as string | undefined)

interface TestimonialItem {
  intro: string
  name: string
  title: string
  tag?: string[]
  image: string
  detail?: string
}

const testimonials = ref<TestimonialItem[] | null>(null)
const alltag = ref<string[] | null>(null)

const show = computed(() =>
  testimonials.value
    ? tag.value
      ? testimonials.value.filter(
          (value: TestimonialItem): boolean =>
            value.tag !== undefined && value.tag.indexOf(String(tag.value)) !== -1
        )
      : testimonials.value
    : undefined
)

onMounted(async () => {
  const data: TestimonialItem[] = await window.api.getYaml('members/members.yml')
  testimonials.value = data
  const curalltag: Set<string> = new Set()
  data.forEach((value: TestimonialItem) => {
    if (value.tag) {
      value.tag.forEach((t: string) => {
        curalltag.add(t)
      })
    }
  })
  alltag.value = Array.from(curalltag)
})

function scrollDownOneScreen(): void {
  window.scrollBy({
    top: window.innerHeight,
    left: 0,
    behavior: 'smooth'
  })
}
</script>
<template>
  <div>
    <div class="z-52 fixed w-full mt-[2em] flex justify-center items-end">
      <div class="max-w-5xl w-full shrink-0 flex-1 flex flex-row justify-center px-[4em]">
        <LiquidBar
          :title="'人员介绍' + (tag ? ` (${tag})` : '') + (show ? `: ${show[idx].name}` : '')"
          back="/"
        ></LiquidBar>
      </div>
    </div>
    <div class="h-[100vh] max-h-[100vh] w-full overflow-hidden flex flex-col">
      <div class="w-full grow-0 flex justify-center min-h-[8em]"></div>
      <div
        class="shrink-0 grow-0 z-10 flex flex-nowrap items-center overflow-x-auto gap-3 z-51 max-w-[80vw] mx-auto pb-[1em] px-[1em]"
      >
        <p class="shrink-0 mr-[0.5em] opacity-[0.8] text-sm">筛选:</p>
        <RouterLink v-for="t in alltag" :key="t" :to="`/member/${t}/0`" class="shrink-0">
          <LiquidGlass :enable-effect="false" container-class="rounded-full">
            <div
              class="flex min-h-8 min-w-8 w-full px-4 items-center justify-center opacity-[0.5] hover:opacity-[0.8] text-sm"
            >
              {{ t }}
            </div>
          </LiquidGlass>
        </RouterLink>
        <RouterLink :to="`/member/0`" class="shrink-0">
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
      <div class="flex-1 min-h-0 max-h-[calc(100vh-14em)]">
        <AnimatedTestimonials
          v-if="show"
          :model-value="idx"
          :testimonials="show"
          :autoplay="false"
          :duration="-1"
        />
      </div>
      <div class="w-full shrink-0 grow-0 flex items-end justify-center h-[6em] min-h-[6em]">
        <div
          v-if="show && show[idx].detail"
          class="mb-[1em] flex flex-col items-center opacity-[0.5] hover:opacity-[0.8] hover:z-54"
        >
          <div class="mb-[0.5em]" @click="scrollDownOneScreen">
            <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24">
              <path
                fill="currentColor"
                d="M5.325 3.95q-.175.625-.25 1.263T5 6.5q0 1.575.45 3.038t1.3 2.762q.2.275.175.6t-.25.55t-.525.2t-.5-.3q-1.05-1.5-1.6-3.25T3.5 6.5q0-.675.075-1.35T3.8 3.8L2.575 5.025q-.225.225-.525.225t-.525-.225T1.3 4.5t.225-.525L3.8 1.7q.3-.3.7-.3t.7.3l2.275 2.275Q7.7 4.2 7.7 4.5t-.225.525t-.525.213t-.525-.213zM16.45 20.825q-.575.2-1.162.188t-1.138-.288L8.5 18.1q-.375-.175-.525-.562T8 16.775l.05-.1q.25-.5.7-.812t1-.363l1.7-.125L8.65 7.7q-.15-.4.025-.763t.575-.512t.762.025t.513.575l3.25 8.925q.175.475-.1.888t-.775.462l-1.175.075L15 18.9q.175.075.375.088t.375-.038l3.925-1.425q.775-.275 1.125-1.038t.075-1.537L19.5 11.2q-.15-.4.025-.763t.575-.512t.762.025t.513.575l1.375 3.75q.575 1.575-.113 3.062T20.375 19.4zm-3-11.675q.4-.15.763.025t.512.575l1.025 2.8q.15.4-.025.775t-.575.525t-.775-.025t-.525-.575l-1-2.825q-.15-.4.025-.763t.575-.512m3.15-.075q.4-.15.763.025t.512.575l.675 1.875q.15.4-.012.763t-.563.512t-.775-.025t-.525-.575L16 10.35q-.15-.4.025-.762t.575-.513m.375 6.05"
              />
            </svg>
          </div>
          <p class="w-full align-left select-none text-xs">查看详情</p>
        </div>
      </div>
    </div>
    <div
      v-if="show && show[idx].detail"
      class="relative w-full overflow-hidden flex flex-col mx-auto justify-center p-[2em] pt-[8em] z-51 bg-white dark:bg-[#0d1117]"
    >
      <div class="max-w-xl w-full mx-auto">
        <MarkdownContent :path="String(show[idx].detail)" />
      </div>
    </div>
  </div>
</template>
