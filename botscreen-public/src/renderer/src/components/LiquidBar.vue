<script lang="ts" setup>
import { LiquidGlass } from '@renderer/components/ui/liquid-glass'
import { useRouter } from 'vue-router'
import PageScrollProg from './PageScrollProg.vue'

interface Props {
  title: string
  back?: string
}

const props = defineProps<Props>()

const router = useRouter()

function goBack(): void {
  if (props.back) {
    router.push(props.back)
  } else {
    router.back()
  }
}
</script>

<template>
  <LiquidGlass
    container-class="z-52 min-h-16 w-full text-black dark:text-white rounded-[16px] px-2"
  >
    <div class="w-full flex-1 flex flex-row items-center justify-center px-1">
      <div class="z-53 w-16 p-4 shrink-0" @click="goBack">
        <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24">
          <path
            fill="currentColor"
            d="M18 11v2h-8l3.5 3.5l-1.42 1.42L6.16 12l5.92-5.92L13.5 7.5L10 11zM2 12A10 10 0 0 1 12 2a10 10 0 0 1 10 10a10 10 0 0 1-10 10A10 10 0 0 1 2 12m2 0a8 8 0 0 0 8 8a8 8 0 0 0 8-8a8 8 0 0 0-8-8a8 8 0 0 0-8 8"
          />
        </svg>
      </div>
      <h1 class="z-53 py-4 grow text-lg font-bold tracking-tight">{{ props.title }}</h1>
      <div class="z-53 w-16 p-4 shrink-0">
        <PageScrollProg />
      </div>
    </div>
  </LiquidGlass>
</template>
