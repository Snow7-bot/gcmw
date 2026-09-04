<script lang="ts" setup>
import { Motion, AnimatePresence } from 'motion-v'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { LiquidGlass } from '@renderer/components/ui/liquid-glass'

const router = useRouter()
const route = useRoute()

interface Testimonial {
  intro: string
  name: string
  title: string
  image: string
}
interface Props {
  modelValue: number
  testimonials?: Testimonial[]
  autoplay?: boolean
  duration?: number
}

const props = withDefaults(defineProps<Props>(), {
  testimonials: () => [],
  autoplay: () => false,
  duration: 5000
})

const active = computed<number>({
  get() {
    const v = Number(route.params.id)
    return Number.isFinite(v) ? v : 0
  },
  set(v) {
    const current = Number(route.params.id)
    if (v === current) return

    router.replace({
      params: {
        ...route.params,
        id: String(v)
      }
    })
  }
})

const WINDOW = 7

const visibleIndexes = computed(() => {
  const total = props.testimonials.length
  const center = active.value

  if (total <= WINDOW * 2 + 1) {
    return Array.from({ length: total }, (_, i) => i)
  }

  const indexes: number[] = []

  for (let offset = -WINDOW; offset <= WINDOW; offset++) {
    const i = (center + offset + total) % total
    indexes.push(i)
  }

  return indexes
})

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const interval = ref<any>()

const activeTestimonialQuote = computed(() => {
  return props.testimonials[active.value].intro.split(' ')
})

onMounted(() => {
  if (props.autoplay) {
    interval.value = setInterval(handleNext, props.duration)
  }
})

onUnmounted(() => {
  if (!interval.value) {
    clearInterval(interval.value)
  }
})

function handleNext(): void {
  active.value = (active.value + 1) % props.testimonials.length
}

function handlePrev(): void {
  active.value = (active.value - 1 + props.testimonials.length) % props.testimonials.length
}

function isActive(index: number): boolean {
  return active.value === index
}

function randomRotateY(): number {
  return Math.floor(Math.random() * 21) - 10
}

function zFor(index: number): number {
  if (index === active.value) return 50
  const total = props.testimonials.length
  const d = Math.min((index - active.value + total) % total, (active.value - index + total) % total)
  return -d
}

function distance(index: number): number {
  const total = props.testimonials.length
  return Math.min((index - active.value + total) % total, (active.value - index + total) % total)
}
</script>

<template>
  <div
    class="mx-auto h-[100%] max-h-[100%] px-4 py-4 font-sans antialiased max-w-6xl md:px-8 lg:px-12"
  >
    <div class="grid gap-16 grid-cols-2 h-[100%] max-h-[100%] min-h-0">
      <div class="h-[100%] max-h-[100%] min-h-0">
        <div class="h-full grid w-full h-[100%] max-h-[100%] min-h-0">
          <AnimatePresence>
            <Motion
              v-for="index in visibleIndexes"
              :key="index"
              as="div"
              :initial="{
                opacity: 0,
                scale: 0.9,
                z: -100,
                rotate: randomRotateY()
              }"
              :animate="{
                opacity: distance(index) === 0 ? 1 : 0.7,
                scale: 1 - Math.min(distance(index) * 0.03, 0.2),
                z: isActive(index) ? 0 : -100,
                rotate: isActive(index) ? 0 : randomRotateY(),
                zIndex: zFor(index),
                y: isActive(index) ? [0, -80, 0] : 0
              }"
              :exit="{
                opacity: 0,
                scale: 0.9,
                z: 100,
                rotate: randomRotateY()
              }"
              :transition="{
                duration: 0.4,
                ease: 'easeInOut'
              }"
              class="col-start-1 row-start-1 inset-0 origin-bottom h-[100%] max-h-[100%] min-h-0"
            >
              <img
                :src="props.testimonials[index].image"
                :alt="props.testimonials[index].name"
                :draggable="false"
                class="size-full rounded-3xl object-cover object-top h-[100%] max-h-[100%]"
              />
            </Motion>
          </AnimatePresence>
        </div>
      </div>
      <div class="flex flex-col justify-between py-4 h-[100%] max-h-[100%] min-h-0">
        <Motion
          :key="active"
          as="div"
          :initial="{
            y: 20,
            opacity: 0
          }"
          :animate="{
            y: 0,
            opacity: 1
          }"
          :exit="{
            y: -20,
            opacity: 0
          }"
          :transition="{
            duration: 0.2,
            ease: 'easeInOut'
          }"
        >
          <h3 class="text-4xl font-bold text-black dark:text-white">
            {{ props.testimonials[active].name }}
          </h3>
          <p class="text-lg text-gray-500 dark:text-neutral-500">
            {{ props.testimonials[active].title }}
          </p>
          <Motion as="p" class="mt-8 text-2xl text-gray-500 dark:text-neutral-300">
            <Motion
              v-for="(word, index) in activeTestimonialQuote"
              :key="index"
              as="span"
              :initial="{
                filter: 'blur(10px)',
                opacity: 0,
                y: 5
              }"
              :animate="{
                filter: 'blur(0px)',
                opacity: 1,
                y: 0
              }"
              :transition="{
                duration: 0.2,
                ease: 'easeInOut',
                delay: 0.02 * index
              }"
              class="inline-block"
            >
              {{ word }}&nbsp;
            </Motion>
          </Motion>
        </Motion>
        <div class="flex gap-8 pt-0">
          <LiquidGlass
            container-class="z-51 group/button flex size-14 items-center justify-center rounded-full"
            @click="handlePrev"
          >
            <div
              class="absolute w-full h-full flex justify-center items-center z-52 text-black transition-transform duration-300 group-hover/button:-rotate-12 dark:text-neutral-400"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24">
                <path
                  fill="none"
                  stroke="currentColor"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  stroke-width="2"
                  d="m12 19l-7-7l7-7m7 7H5"
                />
              </svg>
            </div>
          </LiquidGlass>
          <LiquidGlass
            container-class="z-51 group/button flex size-14 items-center justify-center rounded-full"
            @click="handleNext"
          >
            <div
              class="absolute w-full h-full flex justify-center items-center z-52 text-black transition-transform duration-300 group-hover/button:-rotate-12 dark:text-neutral-400"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24">
                <path
                  fill="none"
                  stroke="currentColor"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  stroke-width="2"
                  d="M5 12h14m-7-7l7 7l-7 7"
                />
              </svg>
            </div>
          </LiquidGlass>
        </div>
      </div>
    </div>
  </div>
</template>
