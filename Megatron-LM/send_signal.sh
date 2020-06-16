machines=($(cat /home/varuna/t-saathl/Varuna/Megatron-LM/available_machines.out))

echo "triggering stop signal"
i=0
while [ $i -lt ${#machines[@]} ]
do
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i ~/.ssh/vdummy.pem varuna@${machines[i]} "cd /home/varuna/t-saathl/Varuna/Megatron-LM; echo \"0\" >nservers; kill -10 \$(cat parent_process)"
    i=$(($i+1))
done
echo "stopped jobs!"