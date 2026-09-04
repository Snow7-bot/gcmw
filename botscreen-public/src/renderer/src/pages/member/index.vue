<script setup lang="ts">
import { useRoute, useRouter, onBeforeRouteUpdate } from 'vue-router'

const route = useRoute()
const router = useRouter()

if (!route.params.id) {
  router.replace('/member/0')
}

onBeforeRouteUpdate((to) => {
  if (!to.params.id) {
    router.replace('/member/0')
  }
})
</script>
