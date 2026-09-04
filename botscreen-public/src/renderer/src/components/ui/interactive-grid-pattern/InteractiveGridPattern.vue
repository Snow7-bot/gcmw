<script lang="ts" setup>
import type { HTMLAttributes } from 'vue'
import { computed, ref } from 'vue'

interface InteractiveGridPatternProps {
  className?: HTMLAttributes['class']
  squaresClassName?: HTMLAttributes['class']
  width?: number
  height?: number
  squares?: [number, number]
  pin?: number[]
}

const props = withDefaults(defineProps<InteractiveGridPatternProps>(), {
  width: 40,
  height: 40,
  squares: () => [24, 24],
  pin: () => []
})

const horizontal = computed(() => props.squares[0])
const vertical = computed(() => props.squares[1])

const totalSquares = computed(() => horizontal.value * vertical.value)

const hoveredSquare = ref<number | null>(null)

const gridWidth = computed(() => props.width * horizontal.value)
const gridHeight = computed(() => props.height * vertical.value)

function getX(index: number): number {
  return (index % horizontal.value) * props.width
}

function getY(index: number): number {
  return Math.floor(index / horizontal.value) * props.height
}

const svgClass = computed(() => [
  'absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 border border-gray-400/30',
  props.className
])

function getRectClass(index: number): string[] {
  return [
    'stroke-gray-400/30 transition-all duration-100 ease-in-out [&:not(:hover)]:duration-1000',
    hoveredSquare.value === index || props.pin.indexOf(index) !== -1
      ? 'fill-gray-300/30'
      : 'fill-transparent',
    props.squaresClassName
  ]
}

function handleMouseEnter(index: number): void {
  hoveredSquare.value = index
  // ipcHandle(index)
}

function handleMouseLeave(): void {
  hoveredSquare.value = null
}
</script>

<template>
  <svg :width="gridWidth" :height="gridHeight" :class="svgClass">
    <rect
      v-for="(_, index) in totalSquares"
      :key="index"
      :x="getX(index)"
      :y="getY(index)"
      :width="width"
      :height="height"
      :class="getRectClass(index)"
      @mouseenter="handleMouseEnter(index)"
      @mouseleave="handleMouseLeave"
    />
  </svg>
</template>
