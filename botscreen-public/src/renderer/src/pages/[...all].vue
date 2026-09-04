<script setup lang="ts">
import { useRouter, useRoute } from 'vue-router'

const router = useRouter()
const route = useRoute()

function goBack(): void {
  router.back()
}
</script>

<template>
  <button @click="goBack">
    <p>{{ route.fullPath }}未完成，返回上页</p>
  </button>
</template>
