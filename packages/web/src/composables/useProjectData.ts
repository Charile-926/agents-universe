import { onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useProjectStore } from '@/stores/project'
import { useFavoritesStore } from '@/stores/favorites'
import { projectsApi } from '@/api/projects'

export function useProjectData() {
  const store = useProjectStore()
  const favoritesStore = useFavoritesStore()
  const router = useRouter()
  const route = useRoute()

  onMounted(async () => {
    if (store.projects.length > 0) return

    try {
      const projects = await projectsApi.getProjects()
      store.setProjects(projects)

      if (projects.length === 0) return

      const routeProjectId = route.params.projectId as string | undefined

      if (routeProjectId) {
        const match = projects.find(p => p.project_id === routeProjectId)
        if (match && !store.currentProject) {
          store.setCurrentProject(match)
        }
      } else if (route.path === '/app') {
        const savedId = store.getSavedProjectId()
        const saved = savedId ? projects.find(p => p.project_id === savedId) : null
        const favorited = favoritesStore.resolvedFavoriteProjects
        // 只在「上次用过的」或「收藏过的」项目之间自动进入。服务端还有项目
        // 但用户没收藏时保持工作台为空，而不是硬塞一个 projects[0]。
        const target = saved ?? favorited[0]
        if (target) {
          store.setCurrentProject(target)
          router.push(`/projects/${target.project_id}/chat`)
        }
      }
    } catch (e) {
      console.error('Failed to load project data', e)
    }
  })
}
