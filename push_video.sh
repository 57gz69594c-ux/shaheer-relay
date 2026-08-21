#!/bin/bash
cd /root/shaheer-project/shaheer-relay
git add ss22_p10f_illustrated.mp4 2>&1
echo "EXIT_ADD=$?"
git commit -m "P10F: Deploy illustrated 3D objects video" 2>&1
echo "EXIT_COMMIT=$?"
git push origin master 2>&1
echo "EXIT_PUSH=$?"
rm -f push_video.sh
