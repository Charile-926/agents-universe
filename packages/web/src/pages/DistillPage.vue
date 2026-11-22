<template>
  <div class="distill-page">
    <div class="distill-page-head">
      <h1 class="distill-page-title">{{ t('skillDistill.title') }}</h1>
      <p class="distill-page-sub">{{ t('skillDistill.subtitle') }}</p>
    </div>
    <div class="distill-page-body">
      <SkillDistillPanel />
    </div>
  </div>
</template>

<script setup lang="ts">
import { useT } from '@/i18n'
import SkillDistillPanel from '@/components/skills/SkillDistillPanel.vue'

const { t } = useT()
</script>
