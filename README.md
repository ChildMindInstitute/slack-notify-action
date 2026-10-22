# slack-notify-action
Custom slack notifier action


Example deployment:

```yaml

      - name: Notify Slack
        if: always()
        uses: ChildMindInstitute/slack-notify-action@v1
        with:
          status: ${{ job.status }}
          webhook-url: ${{ secrets.SLACK_WEBHOOK_URL }}
          template: deploy
          vars: >-
            SERVICE=myapp
            ENVIRONMENT=production
```