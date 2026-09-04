<script setup lang="ts">
import {
  AnimatedModal,
  AnimatedModalBody,
  AnimatedModalContent,
  AnimatedModalFooter
} from '@renderer/components/ui/animated-modal'
import { computed } from 'vue'

interface Props {
  open: boolean
  url: string | null
}

const props = defineProps<Props>()

const emit = defineEmits<{
  (e: 'update:open', v: boolean): void
}>()

const openProxy = computed({
  get: () => props.open,
  set: (v) => emit('update:open', v)
})
</script>

<template>
  <div class="fixed w-100vw h-100vh">
    <AnimatedModal v-model:open="openProxy">
      <AnimatedModalBody
        :lock-scroll="true"
        class="flex w-full h-full max-w-7xl max-h-100vh m-[2em] flex-col"
      >
        <AnimatedModalContent class="flex-1 p-0">
          <iframe v-if="url" :src="url" class="h-full w-full border-none" />
        </AnimatedModalContent>

        <AnimatedModalFooter class="gap-2">
          <p
            class="w-full align-left ml-[1em] select-none text-xs text-gray-600 dark:text-gray-400 opacity-70"
          >
            {{ url }}&emsp;外部链接 · 谨慎浏览
          </p>
        </AnimatedModalFooter>
      </AnimatedModalBody>
    </AnimatedModal>
  </div>
</template>
